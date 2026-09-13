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

```python
import spn_client

result = spn_client.check("https://example.com/some-page")
if result["archived"] is False or result.get("snapshot_stale"):
    submission = spn_client.submit(
        "https://example.com/some-page",
        access_key=ACCESS_KEY,   # optional — omit for anonymous, lower-rate submission
        secret_key=SECRET_KEY,
    )
    if submission["job_id"]:
        # authenticated path: poll for the real outcome
        status = spn_client.check_job_status(
            submission["job_id"], access_key=ACCESS_KEY, secret_key=SECRET_KEY
        )
```

Call `spn_client.reset_rate_limit_state()` once at the start of each
independent run/process if you're running this as a long-lived worker —
the pacing/breaker state is process-wide and intentionally does not reset
itself, so a breaker tripped by one run would otherwise silently degrade
the next.

### Handling a failed capture

```python
status = spn_client.check_job_status(job_id, access_key=ACCESS_KEY, secret_key=SECRET_KEY)
if status["state"] == "failed":
    category = status["retry_category"]  # "permanent" / "transient" / "quota_exhausted" / None
    if category == "quota_exhausted":
        ...  # stop submitting new URLs this run, not just this one
    elif category != "permanent":
        ...  # worth another attempt later (also covers the unknown/None case)
```

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

## License

MIT
