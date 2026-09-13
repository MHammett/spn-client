# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] — 2026-09-13

A senior-engineer audit pass: security, reliability, and packaging hardening,
none of it changing the public API's shape.

### Security
- `capture_capacity()` and `system_status()` now redact their exception text
  before logging it — every other credentialed path already did this, but
  these two logged the raw exception, which can carry the Authorization
  header's credentials (e.g. via `requests.exceptions.InvalidHeader`).
- `check()`'s error path now redacts URL-embedded secrets from its result —
  `url` is fully caller-supplied and may itself carry a query-string
  API key/token for a private page being checked.

### Changed
- Narrowed every `except Exception` in the module to
  `requests.exceptions.RequestException`, so a genuine bug (a `TypeError`
  from bad caller input, for instance) surfaces instead of being reported
  identically to a network hiccup.
- `capture_capacity()`/`system_status()` now distinguish "the request
  failed" from "the response wasn't JSON" — previously both were folded
  into one generic message.
- The authenticated `submit()` path now explicitly rejects an unexpected
  redirect instead of silently following it (matching Internet Archive's
  own official Go client's defensive convention); the anonymous path is
  unaffected — following the redirect there is the whole mechanism.
- `check()`/`submit()` validate `url` up front with a clear `ValueError`
  instead of surfacing a bad input several calls deep as a cryptic
  traceback.

### Added
- Full type hints across the package, with `TypedDict` definitions for
  every function's return shape, plus a `py.typed` marker (PEP 561) so
  consumers get real type-checker support.

## [0.3.0] — 2026-09-13

### Added
- `submit()` gains `capture_cookie` (a Cookie header sent to the *target
  page*, not archive.org), `use_user_agent` (override the capture bot's
  User-Agent), and `delay_wb_availability` (capture becomes visible in the
  Wayback Machine ~12h later instead of immediately) — the remaining
  documented SPN2 capture options not already covered by 0.2.x.
- `check_job_status()` now surfaces `resources` (partial capture progress)
  on `"pending"` and `"failed"` states too, not just `"success"`.

### Changed
- `submit()`'s default `timeout` raised from 30s to 120s, matching
  archive.org's own documented "max total capture duration: 2 minutes" for
  the synchronous anonymous capture path. A shorter default risked
  reporting a slow-but-legitimate capture as `outcome_unknown` when
  archive.org would have finished it given more time.

### Security
- `capture_cookie` is now redacted from error text the same way
  `secret_key`/`target_password` already are.

## [0.2.1] — 2026-09-13

### Added
- `error:no-captures` added to `categorize_job_error()`'s table — observed
  live against real archive.org, absent from archive.org's own SPN2 API
  documentation entirely.

## [0.2.0] — 2026-09-13

### Added
- `submit()` is now paced and circuit-breaker-protected at archive.org's own
  documented capture-endpoint limits (7/min authenticated, 3/min anonymous)
  — previously it skipped the shared pacing clock entirely, and its own 429
  responses never fed back into the breaker every other call respects. A
  confirmed 429 is retried with backoff; an ambiguous `Timeout`/
  `ConnectionError` is not (see `_paced_submit`'s docstring for why).
- `categorize_job_error()` sorts each of SPN2's ~30 documented failure codes
  into `"permanent"` (retrying won't help), `"transient"` (worth trying
  again), or `"quota_exhausted"` (back off the whole run). Surfaced on
  `check_job_status()`'s failed results as `error_code`/`retry_category`.
- `check_job_status()`'s success result now includes `screenshot_url`,
  `duration_seconds`, `resources`, and `outlinks` — previously-discarded
  fields from archive.org's own response.
- `submit()` gains `capture_screenshot`, `outlinks_availability`,
  `skip_first_archive`, `js_behavior_timeout`, `target_username`/
  `target_password` — documented SPN2 capture options exposed as optional
  keyword arguments.

## [0.1.0] — 2026-09-12

Initial extraction. Process-wide rate pacing with a circuit breaker,
dual-mode (anonymous + S3-key-authenticated) capture submission with
async job-status polling, and an explicit archive-outcome vocabulary
(`ARCHIVE_SUBMITTED`/`ARCHIVE_ARCHIVED`/`ARCHIVE_PENDING`/
`ARCHIVE_CAPTURE_FAILED`/`ARCHIVE_SUBMIT_FAILED`/`ARCHIVE_NOT_ATTEMPTED`)
that distinguishes "we asked archive.org" from "archive.org confirmed it
archived this."

[0.3.1]: https://github.com/MHammett/spn-client/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/MHammett/spn-client/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/MHammett/spn-client/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/MHammett/spn-client/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/MHammett/spn-client/releases/tag/v0.1.0
