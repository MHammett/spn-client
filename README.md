# spn-client

A hardened Python client for archive.org's availability API and Save Page Now
(SPN2) — the pieces that are easy to get wrong when you actually run this
against archive.org at any volume:

- **Process-wide rate pacing with a circuit breaker.** archive.org throttles
  per IP, not per thread or per endpoint. A naive per-thread backoff (or none
  at all) looks fine in testing and then silently stops archiving anything
  the first time it's run concurrently or against a busy queue.
- **Both submission modes.** Anonymous `GET /save/<url>` (capture runs inline,
  answer comes back as a redirect) and S3-key-authenticated `POST /save`
  (capture is queued, answer comes back as a `job_id` you poll for).
- **An explicit outcome vocabulary.** `submitted` is not `archived`. This
  library only ever reports `archived: True` alongside a real snapshot URL
  that came back from archive.org — never as a synonym for "we asked."
- **Staleness checking**, so callers can skip re-archiving a URL that already
  has a recent-enough snapshot.
- **Submissions are paced and breaker-protected too**, at archive.org's own
  documented capture-endpoint limits (7/min authenticated, 3/min anonymous)
  — not just the availability check.
- **Categorized capture failures.** archive.org's SPN2 API documents ~30
  specific error codes; `check_job_status()` sorts each into `"permanent"`
  (retrying won't help), `"transient"` (worth trying again), or
  `"quota_exhausted"` (back off the whole run, not just this URL) via
  `categorize_job_error()`.
- **The rest of the documented capture options**, exposed as optional
  keyword arguments on `submit()`: screenshots, outlink availability,
  skipping the first-capture check, JS render timeout, login credentials
  for pages behind a form, a cookie for the target page, a custom capture
  User-Agent, and delayed Wayback availability.
- **A default timeout that matches archive.org's own documented capture
  ceiling** (2 minutes) rather than an arbitrary short one, so a
  slow-but-legitimate anonymous capture doesn't get reported as failed
  when archive.org would have finished it given more time.

Every non-obvious piece of behavior in `client.py` is a documented response to
a specific, dated, measured incident against the real archive.org API — not
a guess.

## Install

```bash
pip install spn-client
```

Using an AI coding assistant? This repo is also servable as an MCP doc
source at `https://gitmcp.io/MHammett/spn-client` (no setup on this end
required — see [`llms.txt`](llms.txt) for the structured summary it reads).

## Usage

The full lifecycle of one URL is three steps: check, submit, then — for the
authenticated path — resolve a pending job on a *later* call. All three
matter; skipping the third is the single most common way to misuse this
library (see "Where the responsibility line is, and why" below).

### 1. Check whether it's already archived

```python
import spn_client

result = spn_client.check(url)
if result["archived"] and not result.get("snapshot_stale"):
    ...  # already durably archived — nothing to do
```

### 2. Submit a capture if it isn't

```python
submission = spn_client.submit(url, access_key=ACCESS_KEY, secret_key=SECRET_KEY)

if submission["archived"]:
    # Anonymous path, or an authenticated capture archive.org resolved
    # synchronously — already confirmed, nothing further to check.
    your_own_storage.mark_archived(url, submission["snapshot_url"])
elif submission["job_id"]:
    # Authenticated path: archive.org queued the job. submitted: True here
    # means "accepted", not "archived" — the capture is not confirmed yet.
    # Save the job_id somewhere you control and check back on a later
    # call; do not treat this as done.
    your_own_storage.save_pending(url, submission["job_id"])
else:
    # submitted: False — archive.org refused, or the request itself
    # failed/timed out. error_summary is safe to show a user; outcome_unknown
    # distinguishes an ambiguous timeout (the capture may have gone through
    # anyway — recheck later) from a real refusal.
    ...
```

### 3. Resolve a pending job (on a later call, not immediately after step 2)

A queued job takes real time to process — checking its status right after
`submit()` will usually still say `"pending"`. Persist the `job_id` from
step 2 and poll it whenever your own schedule next revisits this URL:

```python
status = spn_client.check_job_status(job_id, access_key=ACCESS_KEY, secret_key=SECRET_KEY)

if status["state"] == "success":
    your_own_storage.mark_archived(url, status["snapshot_url"])
elif status["state"] == "failed":
    category = status["retry_category"]  # "permanent" / "transient" / "quota_exhausted" / None
    if category == "permanent":
        your_own_storage.mark_permanently_unavailable(url, status["error_code"])
    elif category == "quota_exhausted":
        ...  # stop submitting new URLs this run, not just this one
    else:
        pass  # transient (also covers the unrecognized/None case) — worth another attempt later
else:
    # "pending" (still queued), "not_checked" (no creds this call, or the
    # breaker tripped), or "unknown" (couldn't read the answer) — nothing
    # new to conclude; leave the job_id in place and ask again later.
    pass
```

`your_own_storage` above is deliberately not part of this library — a flat
file, a database column, an in-memory dict, whatever fits your project.
What matters is tracking exactly these three states (archived /
permanently unavailable / pending-with-a-job-id), rather than collapsing
"submitted" into "archived" — the mistake this three-step shape exists to
prevent.

Call `spn_client.reset_rate_limit_state()` once at the start of each
independent run/process if you're running this as a long-lived worker —
the pacing/breaker state is process-wide and intentionally does not reset
itself, so a breaker tripped by one run would otherwise silently degrade
the next.

### Optional capture options

```python
spn_client.submit(
    url,
    access_key=ACCESS_KEY, secret_key=SECRET_KEY,
    capture_screenshot=True,       # -> status["screenshot_url"] once the job succeeds
    outlinks_availability=True,    # -> status["outlinks"]
    skip_first_archive=True,
    js_behavior_timeout=15,        # seconds, 0-30 (archive.org's default is 5)
    target_username="user", target_password="pass",  # pages behind a login form
    capture_cookie="session=abc123",  # a cookie sent to the *target page*, not archive.org
    use_user_agent="MyBot/1.0",       # what archive.org's capture bot presents to the target site
    delay_wb_availability=True,       # capture is visible in Wayback ~12h later instead of immediately
)
```

Cookie-based session auth to archive.org itself (the alternative to an S3
key pair) is not supported — this client only authenticates with S3 keys,
which archive.org's own docs call "highly preferable" anyway. Don't confuse
that with `capture_cookie` above, which is unrelated: a cookie sent to
whatever page you're asking archive.org to capture, not to archive.org.

### Checking capacity before a big batch

Rate pacing and the circuit breaker already protect against archive.org
push-back reactively. `capture_capacity()` and `system_status()` let a
caller ask proactively, before spending requests — cheaper than finding out
by getting refused:

```python
capacity = spn_client.capture_capacity(access_key=ACCESS_KEY, secret_key=SECRET_KEY)
if capacity["known"] and capacity["daily_exhausted"]:
    ...  # stop submitting for today — quota is used up, not "we're unlucky"

status = spn_client.system_status(access_key=ACCESS_KEY, secret_key=SECRET_KEY)
note = spn_client.service_health_note(status)
if note:
    log.warning("submission failing; %s", note)  # names who's at fault before you go digging
```

`system_status()`/`service_health_note()` are meant to be called once per
run, only after something has already gone wrong — a healthy run pays
nothing for asking. `capture_capacity()` is cheap enough to check before a
large batch, since a wrong or stale reading can only make a run *more*
cautious, never less.

### Comparing an archived copy to the live page

A snapshot URL from `check()`/`submit()`/`check_job_status()` renders with
archive.org's own banner and URL-rewriting shim injected — not useful if
you want to diff the archived bytes against the live page. `snapshot_raw_url()`
gives you the unmodified capture instead:

```python
raw_url = spn_client.snapshot_raw_url(status["snapshot_url"])
```

## What this doesn't do

This is the archive.org client only — it has no opinion about:
- What staleness policy is right for your use case (`stale_days` is always a
  caller-supplied parameter)
- How you track which URLs you've already archived (that's a caller-side
  ledger/cache concern)
- Batch-loop concerns like a progress heartbeat or a wall-clock budget across
  many submissions — those depend on your own operational needs
- Bulk/historical queries (all snapshots of a URL, date-range filtering,
  status-code filtering) — that's what archive.org's separate
  [CDX Server API](https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server)
  is for. This library only ever asks for the single closest snapshot.

### Where the responsibility line is, and why

The rule: **anything whose correct answer depends on archive.org's own API
behavior lives in this library**, because every caller needs the same
correct answer and shouldn't have to rediscover it — which errors are worth
retrying, how fast an endpoint actually tolerates being called, what a
response field means. **Anything whose correct answer depends on what a
specific caller wants to do with that information is a caller-side
concern**, because different callers legitimately want different things
done with the same honest input — how long to keep retrying, when local
state is safe to delete, how a report should phrase "not yet archived."
This library does the first job and deliberately stops short of the
second — see the batch/report examples above.

The most common way to get this wrong from the caller side: treating
`submit()`'s `submitted: True` as if it meant `archived: True`. For the
authenticated path, a successful submission only means archive.org
*queued* the capture — confirming it actually happened means polling
`check_job_status(job_id, ...)` afterward and checking its `state`, not
assuming a queued job succeeded. This library keeps `submitted` and
`archived` as separate fields specifically so that mistake can't happen by
accident — but it can still happen if a caller only reads `submitted` and
never looks at `job_id` or `archived` at all. If you're deciding whether
something is safe to treat as durably archived (safe to prune a local
copy of, cite as a permanent source, etc.), the answer is `archived`
(from `check()` or `submit()`) or a `check_job_status()` call that
returned `state: "success"` — never `submitted` alone.

## References

The two APIs this client wraps, and where its non-obvious behavior comes from:

- [Wayback Machine Availability API](https://archive.org/help/wayback_api.php)
  — the official docs for `check()`. Silent on rate limits; ours were
  measured (see `client.py`'s comments above `_MIN_INTERVAL_SECONDS`).
- [SPN2 Public API docs](https://docs.google.com/document/d/1Nsv52MvSjbLb2PCpHlat0gkzw0EvtSgpKHu4mk0MnrA)
  (Google Doc, not versioned or linked from archive.org's own help page) —
  the source for every `submit()`/`check_job_status()` parameter, response
  field, and the ~30 `status_ext` error codes `categorize_job_error()`
  buckets. Confirmed incomplete in practice — see the issue below.
- [internetarchive/gospn](https://github.com/internetarchive/gospn) — Internet
  Archive's own official Go SPN client. Cross-checked against this library's
  design (see `submit()`'s docstring for where we agree and where we
  deliberately diverge, e.g. on the default timeout).

Issues this project has filed or commented on upstream, with what it found:

- [internetarchive/wayback#274](https://github.com/internetarchive/wayback/issues/274)
  — measured rate-limit data (episodic 429s on the availability API,
  independent of the SPN2 capture-endpoint limits).
- [internetarchive/wayback#297](https://github.com/internetarchive/wayback/issues/297)
  — a corroborating incident for a bug already reported there, plus the
  `outcome_unknown` pattern as a partial client-side workaround.
- [internetarchive/wayback#304](https://github.com/internetarchive/wayback/issues/304)
  — `error:no-captures` is a real SPN2 status code absent from the SPN2 docs
  above; found live, added to `categorize_job_error()` in 0.2.1.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — and [AGENTS.md](AGENTS.md) if an AI
coding agent is doing some or all of the work.

## License

MIT
