"""A hardened client for archive.org's availability and Save Page Now (SPN2) APIs.

Extracted from a citation-verification pipeline where this logic was built and
incident-tested against real archive.org behavior — process-wide rate pacing
with a circuit breaker, dual-mode (anonymous + S3-key-authenticated) capture
submission with async job-status polling, and an outcome vocabulary that
distinguishes "we asked archive.org" from "archive.org confirmed it archived
this." Every non-obvious behavior below is a direct response to a dated,
measured incident, not a speculative design.
"""

import logging
import threading
import time
import re
from datetime import datetime, timezone

import requests

from spn_client._http import DEFAULT_HEADERS
from spn_client import _redact as redact

log = logging.getLogger(__name__)

_AVAILABILITY_API = "https://archive.org/wayback/available"
_SAVE_API = "https://web.archive.org/save"
_SAVE_STATUS_API = "https://web.archive.org/save/status"
_SAVE_USER_STATUS_API = "https://web.archive.org/save/status/user"
_SAVE_SYSTEM_STATUS_API = "https://web.archive.org/save/status/system"

#: Fallback staleness threshold (days) used only when a caller does not pass
#: its own ``stale_days``. Callers with an actual staleness policy should
#: always pass one explicitly — this exists so ``check()``/``submit()``/
#: ``check_job_status()`` have *a* number to compare against rather than
#: raising when none is given.
DEFAULT_STALE_DAYS = 30

# Matches a Wayback Machine snapshot URL and captures its embedded timestamp:
#   https://web.archive.org/web/20250101000000/https://example.com/...
_ARCHIVE_URL_RE = re.compile(
    r"https?://web\.archive\.org/web/(\d{4,14})(?:[a-z_]*)?/", re.IGNORECASE
)


#: What a run actually established about archiving a URL, as opposed to what
#: it *asked* for. The distinction is the whole point of this vocabulary: it
#: is tempting to record only ``submitted: True`` and report "submitted for
#: archiving", which a reader reasonably finishes as "so it is archived".
#: Nothing establishes that on its own. A capture archive.org silently
#: dropped looks exactly like one it completed.
#:
#: ``ARCHIVED`` is the only value that asserts a snapshot exists, and it is
#: only ever set alongside a ``snapshot_url`` that came back from archive.org.
ARCHIVE_SUBMITTED = "submitted"  # accepted; outcome not established (see below)
ARCHIVE_ARCHIVED = "archived"  # a snapshot URL came back — this one is real
ARCHIVE_PENDING = "pending"  # job accepted, capture still running at report time
ARCHIVE_CAPTURE_FAILED = "capture_failed"  # accepted, then the capture failed
ARCHIVE_SUBMIT_FAILED = "submit_failed"  # archive.org refused the request itself
ARCHIVE_NOT_ATTEMPTED = "not_attempted"  # we never asked; the reason is recorded

#: Human-readable phrasing for each outcome, for report output. Kept here so
#: two different renderers cannot drift into describing the same state two
#: different ways.
ARCHIVE_OUTCOME_LABELS = {
    ARCHIVE_SUBMITTED: "submitted; capture outcome not established",
    ARCHIVE_ARCHIVED: "archived",
    ARCHIVE_PENDING: "submitted; capture still pending",
    ARCHIVE_CAPTURE_FAILED: "capture failed",
    ARCHIVE_SUBMIT_FAILED: "submission failed",
    ARCHIVE_NOT_ATTEMPTED: "not submitted",
}


def _age_days_from_timestamp(ts):
    """Days since a Wayback timestamp (YYYYMMDD...), or None if unparseable."""
    if len(ts) >= 8:
        try:
            snap_dt = datetime.strptime(ts[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - snap_dt).days
        except ValueError:
            pass
    return None


# archive.org throttles the availability API at IP level with a long window, not
# per-second. Measured 2026-08-12: 12 consecutive requests all returned 429 —
# including the first — and it was still 429 after a 45s cooldown at one request
# every 6 seconds. A pipeline with no pacing and no backoff at all ended up with
# an archive thread that had been dead for at least two runs: 49 resolved items,
# 0 archived, ~49 rate-limited.
#
# (Re-probed 2026-08-15: six back-to-back lookups all returned 200. The throttle
# is episodic, so the guard has to be always-on rather than tuned to one window.)
#
# Two mechanisms, because one is not enough:
#   * a process-wide minimum interval between calls, serialised on a lock. If
#     callers run this from a thread pool, without this the pool's width
#     decides the request rate.
#   * retry with backoff that honours Retry-After, and a circuit breaker: once
#     archive.org has said 429 repeatedly, further calls in the same run are
#     skipped rather than spending the run's time collecting more 429s.
#
# Every piece of state below is process-wide, and that is the whole design
# constraint: ``check()`` may be called from several worker threads at once.
# Two consequences the first version of this code got wrong, both of which
# silently disabled the protection they were meant to provide:
#
#   * **Per-thread backoff is not backoff.** Sleeping inside the failing thread
#     leaves the other workers hammering archive.org at the full pace. A 429 has
#     to move a clock every thread waits on, so the *process* slows down.
#   * **"Consecutive" is not well defined across interleaved threads.** Resetting
#     a shared counter on success let one worker's 200 erase four other workers'
#     refusals, so a run being throttled 4-in-5 would never trip the breaker —
#     precisely the case it exists for. The count is now a per-run budget that
#     only moves up.
_MIN_INTERVAL_SECONDS = 3.0
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 5.0

#: Refused *lookups* tolerated in a run before we stop asking. Counted once per
#: lookup, not once per attempt, so the number means what it says: the
#: ``_MAX_ATTEMPTS`` retries inside one lookup are a single refusal. (Counting
#: attempts made this trip after two lookups rather than five.)
_CIRCUIT_TRIP_AFTER = 5

#: Queue depth at which archive.org counts as busy rather than merely up.
#: Measured healthy 2026-09-06 with every one of its thirteen capture queues
#: at zero, so any sustained backlog is worth telling the caller about — it
#: is the difference between "your submission failed" and "everyone's did".
_BUSY_QUEUE_DEPTH = 50

_pace_lock = threading.Lock()
_last_call_at = 0.0

#: Absolute ``time.monotonic()`` before which no thread may call. A 429 pushes
#: this forward, which is what makes the backoff process-wide.
_blocked_until = 0.0

#: Lookups this run that exhausted every attempt against a 429. Never reset on
#: success — see the note above on why "consecutive" cannot work here.
_rate_limited_lookups = 0


def reset_rate_limit_state():
    """Clear the pacing clock, the backoff, and the circuit breaker.

    Call this at the start of every run. The state is process-wide, so without
    a reset a breaker tripped by one run would skip every archive.org call in
    the next run inside the same process — a silently degraded run whose
    submissions all report "skipped" for a limit that expired long ago.
    """
    global _last_call_at, _blocked_until, _rate_limited_lookups
    with _pace_lock:
        _last_call_at = 0.0
        _blocked_until = 0.0
        _rate_limited_lookups = 0


def rate_limited_out():
    """True once the circuit breaker has tripped for this run."""
    with _pace_lock:
        return _rate_limited_lookups >= _CIRCUIT_TRIP_AFTER


def _pace():
    """Block until the shared clock allows another call.

    Waits for whichever is later: ``_MIN_INTERVAL_SECONDS`` since the last call,
    or the end of a backoff that a 429 imposed on every thread. Sleeping while
    holding the lock is deliberate — it is exactly what makes the interval
    process-wide instead of per-thread.
    """
    global _last_call_at
    with _pace_lock:
        now = time.monotonic()
        target = max(_last_call_at + _MIN_INTERVAL_SECONDS, _blocked_until)
        if target > now:
            time.sleep(target - now)
        _last_call_at = time.monotonic()


def _note_rate_limited(retry_after):
    """Back every thread off after a 429, not just the one that hit it."""
    global _blocked_until
    with _pace_lock:
        _blocked_until = max(_blocked_until, time.monotonic() + retry_after)


def _note_lookup_refused():
    """Count one lookup that never got past a 429."""
    global _rate_limited_lookups
    with _pace_lock:
        _rate_limited_lookups += 1


def _retry_after_seconds(resp, attempt):
    """Seconds to wait before retrying, preferring the server's own answer."""
    header = (resp.headers or {}).get("Retry-After") if resp is not None else None
    if header:
        try:
            return min(float(header), 60.0)
        except (TypeError, ValueError):
            pass
    return _BACKOFF_BASE_SECONDS * (2**attempt)


def _paced_get(endpoint, timeout, params=None, headers=None):
    """GET an archive.org endpoint with pacing, backoff, and a circuit breaker.

    Raises the last exception if every attempt fails, so the caller's existing
    error handling is unchanged.

    There is no local sleep between attempts: a 429 pushes the shared clock out
    and the wait then happens in the next ``_pace()``, which every thread goes
    through. That is the difference between the process backing off and one
    thread backing off while seven others keep the pressure on.

    Endpoint-agnostic on purpose. The availability API and the Save Page Now
    job-status API are the same host, the same IP-level throttle, and the same
    consequence for ignoring it, so they share one pacing clock and one breaker
    budget rather than each getting its own. Two schemes would each be pacing
    against half the real request rate, which is how you end up rate-limited by
    a system that believes it is being polite.
    """
    last_exc = None
    rate_limited = False
    for attempt in range(_MAX_ATTEMPTS):
        _pace()
        try:
            resp = requests.get(
                endpoint,
                params=params,
                timeout=timeout,
                headers=headers or DEFAULT_HEADERS,
            )
        except Exception as exc:
            last_exc = exc
            continue
        if resp.status_code == 429:
            rate_limited = True
            last_exc = requests.HTTPError(
                f"429 Client Error: Too Many Requests for url: {resp.url}",
                response=resp,
            )
            _note_rate_limited(_retry_after_seconds(resp, attempt))
            continue
        resp.raise_for_status()
        return resp
    # One refused lookup, however many attempts it took to establish that.
    if rate_limited:
        _note_lookup_refused()
    raise last_exc


def _get_availability(url, timeout):
    """GET the availability API through the shared pacing/backoff/breaker path."""
    return _paced_get(_AVAILABILITY_API, timeout, params={"url": url})


def _transport_failure_summary(exc, what):
    """One reader-facing sentence for an archive.org call that did not complete.

    The raw exception is for the log, not for whoever reads a report built on
    this. Left unfiltered it reaches the caller as
    ``HTTPSConnectionPool(host='web.archive.org', port=443): Max retries
    exceeded with url: /save/status/... (Caused by NewConnectionError(...
    [WinError 10061] ...))`` — observed verbatim in a real run 2026-09-06. That
    is a debugger's string in a message meant for someone deciding what to do
    next, and it invites them to debug the networking instead of telling them
    what it means for their submission.

    Callers keep the raw text under a separate key so nothing is lost.
    """
    if isinstance(exc, requests.exceptions.Timeout):
        return f"archive.org did not answer {what} within the timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return (
            f"could not reach archive.org {what} — the connection was refused, "
            f"dropped, or the host did not resolve"
        )
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status:
        return f"archive.org answered HTTP {status} {what}"
    return f"the request to archive.org {what} failed"


def snapshot_raw_url(snapshot_url):
    """The ``id_`` form of a snapshot URL: the original captured bytes.

    ``https://web.archive.org/web/<ts>/<url>`` serves the capture with
    archive.org's own banner and its ``wombat.js`` URL-rewriting shim injected.
    Appending ``id_`` to the timestamp — ``/web/<ts>id_/<url>`` — serves what was
    actually captured, unmodified.

    That difference is what makes an archived copy checkable against the live
    page at all. Measured 2026-09-06 across three URLs, one of them archived a
    week earlier: the ``id_`` body was **byte-identical** to the live page
    (SHA-256 equal, 6639/10923/9569 bytes), while the ordinary form was nearly
    three times the size for the same document. Comparing the injected form
    would be comparing archive.org's chrome, and would report every page as
    divergent from itself.

    Returns ``None`` if ``snapshot_url`` is not a snapshot URL.
    """
    if not snapshot_url:
        return None
    m = _ARCHIVE_URL_RE.search(snapshot_url)
    if not m:
        return None
    # Replace the matched "/web/<ts><flags>/" with "/web/<ts>id_/".
    return (
        snapshot_url[: m.start()]
        + f"https://web.archive.org/web/{m.group(1)}id_/"
        + snapshot_url[m.end() :]
    )


def _snapshot_state(snapshot_url, ts, stale_days=None):
    """The four snapshot fields every caller reports, from a URL and timestamp.

    One implementation because ``check()``, ``submit()`` and
    ``check_job_status()`` all have to answer "how old is this snapshot, and
    is that too old" the same way. They previously could disagree because only
    one of them ever answered it; now that a submission can come back with a
    real snapshot, they share the arithmetic instead of risking that.
    """
    threshold = stale_days if stale_days is not None else DEFAULT_STALE_DAYS
    age = _age_days_from_timestamp(ts) if ts else None
    return {
        "snapshot_url": snapshot_url,
        "snapshot_ts": ts,
        "snapshot_age_days": age,
        "snapshot_stale": age is not None and age > threshold,
    }


def check(url, timeout=10, stale_days=None):
    """Check if a URL has a Wayback Machine snapshot and how fresh it is.

    If ``url`` is itself a Wayback Machine snapshot link, it is recognized as
    already-archived: the snapshot date is read from the URL's embedded
    timestamp (no archive-of-an-archive lookup), and ``is_archive_url`` is set.
    Whether that archive link actually resolves is a separate question this
    function does not answer.

    Returns a dict with:
      archived        bool | None  — True/False, or None on network error
      is_archive_url  bool         — True when the link itself is a web.archive.org URL
      snapshot_url    str          — direct https://web.archive.org/web/... URL
      snapshot_ts     str          — raw Wayback timestamp (YYYYMMDDHHMMSS)
      snapshot_age_days  int       — days since the snapshot was taken
      snapshot_stale  bool         — True when older than the stale threshold
      error           str          — set only on network/parse failure
    """
    # The link is already a Wayback snapshot — read its date from the URL itself.
    m = _ARCHIVE_URL_RE.search(url)
    if m:
        result = {"url": url, "archived": True, "is_archive_url": True}
        result.update(_snapshot_state(url, m.group(1), stale_days))
        return result

    # Once archive.org has refused repeatedly, stop asking: every further call
    # costs the run several seconds of pacing to collect another 429.
    if rate_limited_out():
        return {
            "url": url,
            "archived": None,
            "error": (
                "skipped: archive.org rate limit tripped earlier this run "
                "(no snapshot lookup attempted)"
            ),
        }
    try:
        resp = _get_availability(url, timeout)
    except Exception as exc:
        log.debug("Wayback availability check failed for %s: %s", url, exc)
        return {"url": url, "archived": None, "error": str(exc)}

    try:
        data = resp.json()
    except ValueError as exc:
        # A 200 that isn't JSON means archive.org served something other than an
        # availability answer — an error or challenge page. Reporting the raw
        # decoder message ("Expecting value: line 1 column 1") sends the reader
        # hunting for a parser bug that isn't there; it is the same misdirection
        # upstream reported as akamhy/waybackpy#200, where a throttled lookup
        # surfaces as invalid JSON. Say what actually arrived instead.
        log.debug("Wayback availability returned non-JSON for %s: %s", url, exc)
        return {
            "url": url,
            "archived": None,
            "error": (
                f"archive.org returned a non-JSON {resp.status_code} response "
                f"({len(resp.content)} bytes) from the availability API"
            ),
        }

    closest = data.get("archived_snapshots", {}).get("closest", {})
    if not closest.get("available"):
        return {"url": url, "archived": False}

    result = {"url": url, "archived": True}
    # The availability API reports the captured response's own HTTP status, and
    # it's tempting to discard it. It matters: a snapshot can be a capture of a
    # 403 block page or a 404, and "a snapshot exists" then means the opposite
    # of what a caller might assume. A capture made through ``submit()`` here
    # always sends ``capture_all=0`` so it never makes one, but a *pre-existing*
    # snapshot is outside this package's control.
    status = closest.get("status")
    if status:
        result["snapshot_status"] = str(status)
        result["snapshot_is_error_capture"] = not str(status).startswith("2")
    result.update(
        _snapshot_state(
            closest.get("url", ""), closest.get("timestamp", ""), stale_days
        )
    )
    return result


#: Save Page Now capture options this client sets deliberately.
#:
#: Read off the live ``/save`` form 2026-09-06 — its control names *are* the API
#: parameter names: ``capture_outlinks``, ``capture_all``, ``capture_screenshot``,
#: ``disable_adblocker``, ``wm-save-mywebarchive``, ``email_result``, ``wacz``.
#: All of them work on the authenticated endpoint. Only one is set, and it's a
#: correctness fix rather than a feature:
#:
#: ``capture_all=0`` — **the form defaults this ON**, and on means "archive the
#:   page even if it answers 4xx/5xx". For most durability use cases that is a
#:   way to manufacture a lie: a source that blocks the capture with a 403
#:   would get its block page archived, a later ``check()`` would find a
#:   snapshot, and a caller would report the URL as archived when what's
#:   archived is an error page. It bites hardest on exactly the sources that
#:   refuse automated requests — the ones most needing an archive. A capture
#:   that fails should be *reported* as failed, which is what this module does.
#:
#: Deliberately NOT set, and why:
#:   ``email_result`` / ``wacz`` — archive.org emails the account owner, once
#:     per capture. A run submits many URLs; enabling either turns a batch job
#:     into an inbox full of mail nobody asked for.
#:   ``wm-save-mywebarchive`` — writes to the operator's personal archive. Their
#:     account, their choice, not a side effect of calling this library.
#:   ``capture_outlinks`` — every outlink of every submitted URL is an enormous
#:     load increase on a service that already throttles callers, for pages
#:     nothing asked to capture.
#:   ``capture_screenshot`` — a second form of evidence, and a real option worth
#:     revisiting, but this package renders nothing from it today.
#:   ``force_get`` — trades the headless browser for a plain GET, which captures
#:     JavaScript-rendered pages worse. The point is a faithful copy.
_CAPTURE_OPTIONS = {"capture_all": "0"}


def _capture_params(url, stale_days=None):
    """Form fields for one authenticated capture request.

    ``if_not_archived_within`` is deliberately NOT sent, and it is worth saying
    why because it looks like an obvious fit. It asks archive.org to skip the
    capture when a snapshot newer than N already exists — seemingly the same
    idea as a caller's own staleness policy.

    Tried against the live API 2026-09-06. Sending ``if_not_archived_within``
    for a page with a recent snapshot returns::

        {"url": "...", "job_id": null,
         "message": "The same snapshot had been made 177 hours, 9 minutes ago.
                     You can make new capture of this URL after 4320 hours."}

    while the identical request without it captures normally. So the skip is
    caused entirely by the parameter — archive.org imposes no such restriction
    of its own — and it leaves the caller with no job id and no snapshot URL,
    i.e. less information than before.

    More importantly it is a *second gate on the same decision*. This module
    only submits a URL when ``check()`` already said it has no snapshot or a
    stale one; asking archive.org to independently re-decide that can only
    produce disagreement between two rules meant to answer one question.

    ``stale_days`` is accepted so callers need not care which options apply.
    """
    return {"url": url, **_CAPTURE_OPTIONS}


def submit(url, timeout=30, access_key=None, secret_key=None, stale_days=None):
    """Request that archive.org capture and archive ``url`` (Save Page Now / SPN2).

    Returns what was *established*, not merely what was asked for. The two
    paths establish different amounts, and the difference is not a detail:

    **Unauthenticated** (``GET /save/<url>``, no credentials): archive.org runs
    the capture inline and answers with a ``302`` to the resulting snapshot.
    Measured 2026-09-05 against a real URL: one redirect hop to a snapshot with
    a timestamp minted seconds earlier. So on this path the snapshot URL is in
    hand *before this function returns* — no job id, no polling, nothing to
    wait for. A naive implementation that follows the redirect and discards
    the final URL, reporting only ``submitted: True``, throws away an answer
    archive.org has already given.

    **Authenticated** (``POST /save`` with an S3-style key pair from
    https://archive.org/account/s3.php): the capture is queued and the response
    carries a ``job_id`` instead of a snapshot. That id is the only handle on
    the outcome, and ``check_job_status`` is the only thing that can read it.

    Either way this does not block waiting for a capture — callers decide who
    waits, how long, and why (see ``check_job_status``).

    Returns a dict with:
      url               str
      submitted         bool       — archive.org accepted the request
      job_id            str | None — SPN2 job id (authenticated submission only)
      archived          bool       — set True only when a snapshot URL came back
      snapshot_url      str        — present only when ``archived``
      snapshot_ts       str        — raw Wayback timestamp (YYYYMMDDHHMMSS)
      snapshot_age_days int | None
      snapshot_stale    bool       — a redirect can land on a pre-existing
                                     snapshot rather than a fresh capture, so
                                     freshness is measured, never assumed
      outcome_unknown   bool       — set with ``error`` when the request went
                                     out and we stopped listening before an
                                     answer came back. Not the same fact as a
                                     refusal: archive.org may well have run the
                                     capture anyway, so this must not be
                                     reported as a failed submission.
      error             str        — set only on failure
    """
    # The breaker exists because archive.org throttles per IP across endpoints.
    # A naive submission path ignores it: once five lookups had been refused,
    # the availability API goes quiet while Save Page Now — the *more*
    # expensive call, since it starts a real capture — keeps firing at full
    # pace. Honouring it here is reuse of the one scheme, not a second one.
    if rate_limited_out():
        return {
            "url": url,
            "submitted": False,
            "job_id": None,
            "archived": False,
            "error": (
                "skipped: archive.org rate limit tripped earlier this run "
                "(no capture requested)"
            ),
            "error_summary": (
                "not requested — archive.org had already rate-limited this run"
            ),
            "rate_limited": True,
        }

    headers = dict(DEFAULT_HEADERS)
    try:
        if access_key and secret_key:
            headers["Authorization"] = f"LOW {access_key}:{secret_key}"
            headers["Accept"] = "application/json"
            resp = requests.post(
                _SAVE_API,
                data=_capture_params(url, stale_days),
                headers=headers,
                timeout=timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
            result = {
                "url": url,
                "submitted": True,
                "job_id": payload.get("job_id"),
                "archived": False,
            }
            if not result["job_id"]:
                # Accepted, but no capture started. archive.org explains itself
                # in ``message`` (e.g. "The same snapshot had been made 177
                # hours ago"). Carrying that through is the difference between
                # the caller knowing why nothing happened and knowing nothing.
                message = str(payload.get("message") or "").strip()
                result["error_summary"] = (
                    f"archive.org accepted the request without starting a "
                    f"capture: {message}"
                    if message
                    else "archive.org accepted the request but started no capture"
                )
            return result

        resp = requests.get(
            f"{_SAVE_API}/{url}",
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        result = {"url": url, "submitted": True, "job_id": None, "archived": False}
        # The capture archive.org just ran, named by the URL it redirected us to.
        m = _ARCHIVE_URL_RE.search(resp.url or "")
        if m:
            result["archived"] = True
            result.update(_snapshot_state(resp.url, m.group(1), stale_days))
        return result
    except Exception as exc:
        err = redact.redact_value(redact.redact_url_keys(str(exc)), secret_key)
        # A read timeout is not a refusal. The request reached archive.org and
        # we gave up waiting for the answer; the capture may have run to
        # completion regardless. Observed live 2026-09-05 — a 30s read timeout
        # on a save that archive.org had almost certainly accepted. Calling
        # that "submission failed" overstates it in the other direction, the
        # same way calling it "archived" would.
        timed_out = isinstance(exc, requests.exceptions.Timeout)
        log.warning(
            "Wayback submission %s for %s: %s",
            "timed out" if timed_out else "failed",
            url,
            err,
        )
        result = {
            "url": url,
            "submitted": False,
            "job_id": None,
            "error": err,
            # The sentence a caller is safe to surface to an end user.
            # ``error`` stays raw (redacted) for logging.
            "error_summary": _transport_failure_summary(exc, "when asked to capture"),
        }
        if timed_out:
            result["outcome_unknown"] = True
        return result


def check_job_status(
    job_id, timeout=15, access_key=None, secret_key=None, stale_days=None
):
    """Read the outcome of an SPN2 capture job. Never raises.

    **This endpoint is credential-only.** Probed 2026-09-05: both
    ``GET /save/status/<job_id>`` and the bare ``GET /save/status`` answer
    ``401 {"message": "You need to be logged in to use Save Page Now."}``
    without an ``Authorization`` header, and answer the same to a well-formed
    but wrong ``LOW`` credential. There is therefore no unauthenticated way to
    find out how a capture went, which is exactly why the unauthenticated
    submission path reads its answer off the redirect instead.

    Goes through the same ``_paced_get`` as the availability lookup, so it
    shares one pacing clock, one backoff and one breaker budget with every
    other archive.org call in the process — per this module's rate-limit
    design, which exists because archive.org throttles per IP, not per
    endpoint.

    Returns a dict with:
      job_id            str
      state             str  — one of:
                                "success"     capture completed; snapshot fields set
                                "pending"     archive.org is still working on it
                                "failed"      archive.org tried and could not
                                "not_checked" we did not ask (no creds, breaker
                                              tripped) — asserts nothing either way
                                "unknown"     we asked and could not read the answer
      reason            str | None — why, for every state except "success"
      snapshot_url      str        — present only on "success"
      snapshot_ts       str
      snapshot_age_days int | None
      snapshot_stale    bool
    """
    if not job_id:
        return {
            "job_id": job_id,
            "state": "not_checked",
            "reason": "no SPN2 job id was recorded for this submission",
        }
    if not (access_key and secret_key):
        return {
            "job_id": job_id,
            "state": "not_checked",
            "reason": (
                "archive.org's job-status endpoint requires credentials (it "
                "answers 401 without them) — pass access_key/secret_key to "
                "have capture outcomes verified"
            ),
        }
    # Same reasoning as check(): once archive.org has refused repeatedly, every
    # further call costs the run seconds of pacing to collect another 429.
    if rate_limited_out():
        return {
            "job_id": job_id,
            "state": "not_checked",
            "reason": (
                "skipped: archive.org rate limit tripped earlier this run "
                "(no job-status lookup attempted)"
            ),
        }

    headers = dict(DEFAULT_HEADERS)
    headers["Authorization"] = f"LOW {access_key}:{secret_key}"
    headers["Accept"] = "application/json"
    try:
        resp = _paced_get(f"{_SAVE_STATUS_API}/{job_id}", timeout, headers=headers)
    except Exception as exc:
        err = redact.redact_value(redact.redact_url_keys(str(exc)), secret_key)
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code == 401:
            # Verified shape, not a guess — see this function's docstring.
            err = (
                "archive.org rejected the credentials for the job-status "
                "endpoint (401). The capture outcome is unknown, not failed."
            )
        log.debug("Wayback job status lookup failed for %s: %s", job_id, err)
        if status_code == 401:
            return {"job_id": job_id, "state": "unknown", "reason": err}
        return {
            "job_id": job_id,
            "state": "unknown",
            "reason": _transport_failure_summary(exc, "about this capture"),
            "raw_error": err,
        }

    try:
        data = resp.json()
    except ValueError:
        # Same misdirection guard as check(): a 200 that isn't JSON is an error
        # or challenge page, and reporting the decoder's complaint sends the
        # reader hunting for a parser bug that isn't there.
        return {
            "job_id": job_id,
            "state": "unknown",
            "reason": (
                f"archive.org returned a non-JSON {resp.status_code} response "
                f"({len(resp.content)} bytes) from the job-status API"
            ),
        }

    status = str(data.get("status") or "").strip().lower()
    if status == "success":
        ts = str(data.get("timestamp") or "")
        original = str(data.get("original_url") or "")
        if not ts or not original:
            # Reported success without naming what it captured. Don't invent a
            # snapshot URL out of half an answer.
            return {
                "job_id": job_id,
                "state": "unknown",
                "reason": (
                    "archive.org reported the capture succeeded but named no "
                    "timestamp/original_url, so no snapshot URL can be given"
                ),
            }
        result = {"job_id": job_id, "state": "success", "reason": None}
        result.update(
            _snapshot_state(
                f"https://web.archive.org/web/{ts}/{original}", ts, stale_days
            )
        )
        return result
    if status == "pending":
        return {
            "job_id": job_id,
            "state": "pending",
            "reason": "archive.org has not finished this capture yet",
        }
    if status == "error":
        # SPN2 spreads the explanation over three optional fields; take the most
        # human one present rather than whichever happens to be first.
        detail = (
            data.get("message") or data.get("status_ext") or data.get("exception") or ""
        )
        return {
            "job_id": job_id,
            "state": "failed",
            "reason": str(detail).strip()
            or "archive.org reported an unspecified error",
        }
    return {
        "job_id": job_id,
        "state": "unknown",
        "reason": (
            f"archive.org reported an unrecognized job status {status!r}"
            if status
            else "archive.org's job-status response carried no status field"
        ),
    }


def capture_capacity(timeout=15, access_key=None, secret_key=None):
    """How much Save Page Now capacity this account has right now. Never raises.

    ``GET /save/status/user``, credential-only like the job-status endpoint.
    Measured live 2026-09-06::

        {"processing":0,"available":3,"daily_captures":49,"daily_captures_limit":30000}

    Why this is worth a request: every concurrency number governing submission
    is otherwise invented, picked by watching archive.org get upset, and static
    — it cannot tell a run with three free capture slots from a run with none.
    This endpoint answers the question directly, and it answers it *before*
    the requests are spent rather than after, which is the difference between
    pacing and apologising. The circuit breaker stays exactly where it is;
    this only narrows what a caller attempts in the first place.

    Deliberately never *raises* the concurrency ceiling a caller might derive
    from it — a reading can only make a run more cautious, so a wrong or stale
    answer cannot make things worse.

    Returns a dict with:
      available            int | None — concurrent capture slots free now
      processing            int | None — captures this account has in flight
      daily_captures       int | None
      daily_captures_limit int | None
      daily_exhausted      bool       — quota is used up; submitting is pointless
      known                bool       — False when we could not find out
      reason               str | None — why not, when ``known`` is False
    """
    unknown = {
        "available": None,
        "processing": None,
        "daily_captures": None,
        "daily_captures_limit": None,
        "daily_exhausted": False,
        "known": False,
    }
    if not (access_key and secret_key):
        return {
            **unknown,
            "reason": (
                "archive.org reports capture capacity only to an authenticated "
                "account; pass access_key/secret_key"
            ),
        }
    if rate_limited_out():
        return {
            **unknown,
            "reason": (
                "skipped: archive.org rate limit tripped earlier this run "
                "(no capacity lookup attempted)"
            ),
        }

    headers = dict(DEFAULT_HEADERS)
    headers["Authorization"] = f"LOW {access_key}:{secret_key}"
    headers["Accept"] = "application/json"
    try:
        resp = _paced_get(_SAVE_USER_STATUS_API, timeout, headers=headers)
        data = resp.json()
    except Exception as exc:
        log.debug("Wayback capture-capacity lookup failed: %s", exc)
        return {
            **unknown,
            "reason": _transport_failure_summary(exc, "for capture capacity"),
        }

    def _int(key):
        value = data.get(key)
        return value if isinstance(value, int) else None

    used, limit = _int("daily_captures"), _int("daily_captures_limit")
    return {
        "available": _int("available"),
        "processing": _int("processing"),
        "daily_captures": used,
        "daily_captures_limit": limit,
        # Only assert exhaustion when both numbers are real. "Unknown" must not
        # collapse into "you are out of quota" and stop a run submitting.
        "daily_exhausted": bool(
            used is not None and limit is not None and used >= limit
        ),
        "known": True,
        "reason": None,
    }


def system_status(timeout=15, access_key=None, secret_key=None):
    """Is archive.org's capture system healthy, or are we the problem? Never raises.

    ``GET /save/status/system``. Measured live 2026-09-06::

        {"recent_captures":941,"status":"ok","queues":{"spn2-captures":0, ...13 queues}}

    Why this is worth asking. When submission degrades, everything a caller
    can see looks the same from the inside: a 520, a read timeout, a refused
    connection. Those are equally consistent with "we asked too often" and with
    "the service is having a bad afternoon" — the same misdiagnosis as treating
    a User-Agent block as rate limiting. The fix for one is to back off, and
    the fix for the other is to wait and stop blaming yourself.

    Meant to be asked once per run and only when something has already gone
    wrong, so a healthy run pays nothing for it.

    Returns a dict with:
      ok               bool | None — archive.org's own health verdict
      status           str        — the raw status string it reported
      recent_captures  int | None
      busiest_queue    (name, depth) | None — the deepest non-empty queue
      known            bool       — False when we could not find out
      reason           str | None
    """
    unknown = {
        "ok": None,
        "status": "",
        "recent_captures": None,
        "busiest_queue": None,
        "known": False,
    }
    if rate_limited_out():
        # Deliberately still asks nothing: the breaker exists because further
        # calls cost the run pacing budget, and that applies to diagnosis too.
        return {
            **unknown,
            "reason": (
                "skipped: archive.org rate limit tripped earlier this run "
                "(no service-status lookup attempted)"
            ),
        }

    headers = dict(DEFAULT_HEADERS)
    headers["Accept"] = "application/json"
    if access_key and secret_key:
        headers["Authorization"] = f"LOW {access_key}:{secret_key}"
    try:
        resp = _paced_get(_SAVE_SYSTEM_STATUS_API, timeout, headers=headers)
        data = resp.json()
    except Exception as exc:
        log.debug("Wayback system-status lookup failed: %s", exc)
        return {
            **unknown,
            "reason": _transport_failure_summary(exc, "for its service status"),
        }

    status = str(data.get("status") or "").strip()
    queues = data.get("queues")
    busiest = None
    if isinstance(queues, dict):
        depths = [(n, d) for n, d in queues.items() if isinstance(d, int) and d > 0]
        if depths:
            busiest = max(depths, key=lambda kv: kv[1])
    recent = data.get("recent_captures")
    return {
        "ok": status.lower() == "ok",
        "status": status,
        "recent_captures": recent if isinstance(recent, int) else None,
        "busiest_queue": busiest,
        "known": True,
        "reason": None,
    }


def service_health_note(status):
    """One clause naming who was at fault, or None when it adds nothing.

    Returns None for a healthy service *and* for an unknown one: appending
    "we could not tell" to every failed submission would be noise on top of a
    failure the caller is already looking at.
    """
    if not status or not status.get("known"):
        return None
    if status.get("ok"):
        busiest = status.get("busiest_queue")
        if busiest and busiest[1] >= _BUSY_QUEUE_DEPTH:
            return (
                f"archive.org reported itself healthy but busy at the time "
                f"({busiest[0]} queue {busiest[1]} deep)"
            )
        return "archive.org reported its capture system healthy at the time"
    return (
        f"archive.org reported its capture system as "
        f"{status.get('status') or 'not ok'} at the time — this was the service, "
        f"not the caller"
    )
