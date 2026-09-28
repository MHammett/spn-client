# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Landing a change and shipping a release are two separate decisions here —
see `[Unreleased]` below. Changes accumulate there until the maintainer
decides a batch is tested and coherent enough to become a real version;
see `AGENTS.md` if you're an AI coding agent about to add an entry here.

## [Unreleased]

### Added
- `check(cdx_fallback=True, cdx_timeout=60)`: when the availability API says a
  URL is "not archived" or "stale", ask the CDX index for the newest capture
  and use it if it is newer and real. **Off by default; no behavior changes
  unless a caller opts in.** Motivated by the availability API never returning
  a `warc/revisit` record, which is archive.org's pointer for unchanged
  content, so an unchanged page cannot become fresh through `check()` however
  often it is captured, and a caller acting on "stale" re-submits it forever.
  Measured 2026-09-28 for one NHTSA PDF: CDX lists a 2024-08-12 capture (200,
  digest `DMH3E3XE…`) and a 2026-09-27 20:42:13 UTC `warc/revisit` record with
  the *same* digest whose exact-timestamp URL answers `200 application/pdf`,
  while the availability API returned only the 2024 capture.
  A revisit is trusted only after a second lookup finds a `200` capture with
  the same digest, so a page that has answered 404 for a year is not reported
  freshly archived by its latest revisit. CDX can only promote an answer, never
  demote one; a failed lookup is reported in `cdx_error` and changes nothing
  else. New result keys: `found_via`, `snapshot_is_revisit`, `cdx_error`.
  It is opt-in because CDX is slow: of seven measured queries four answered in
  15.6s to 51.7s and three timed out, and one more got `503 Temporarily
  Offline`. It has its own timeout and is not retried.
  **Not a fix for the availability API's lag right after a capture** (archive.org
  documents it, and a third-party spec measured CDX empty for the capture day
  too); that needs a different answer.

### Fixed
- `submit()` now reports a refusal as a refusal. archive.org answers a
  capture request it will not run — most visibly a URL that has hit its
  per-day capture cap — with HTTP 200 and a JSON body of
  `{"status": "error", "status_ext": ..., "message": ...}` and no `job_id`.
  `submit()` read only `job_id` and `message`, so it returned
  `submitted: True` with the reason buried in `error_summary` ("accepted the
  request without starting a capture"), which a caller reading `submitted`
  took for success. It now returns `submitted: False` and surfaces
  `error_code` (the raw `status_ext`) and `retry_category`, the same fields
  `check_job_status()` already gives a failed job.
  **Behavior change:** `submitted` is `False` for these responses where it
  used to be `True`. The one body that is still "accepted, nothing started" —
  a null `job_id` and a `message` with no `status`, which is what
  `if_not_archived_within` produced when measured 2026-09-06 — is unchanged.
  Evidence for the body's shape is in `submit()`'s docstring: the message was
  observed live 2026-09-27, the field names are as three unrelated clients
  record them, and a refusal is recognised by either marker because we have
  not captured the raw body ourselves.
- A refusal's message is now redacted of `secret_key`, `target_password`,
  `capture_cookie` and URL-embedded keys before it is returned, by the same
  helper the transport-failure path uses, so the two cannot drift apart.

### Changed
- `error:too-many-daily-captures` is now categorized `"transient"`, not
  `"quota_exhausted"`. It is a cap on one URL (SPN2's own wording: "This URL
  has been captured 10 times today"), not on the account, so
  `"quota_exhausted"` — which tells a caller to stop submitting everything —
  was halting runs that had nothing else wrong with them. `error_code` still
  names the cause for a caller that wants to retry that URL tomorrow. The
  evidence is in the comment on its `_JOB_ERROR_CATEGORIES` entry.
  **Behavior change** for any caller that branched on `"quota_exhausted"` to
  stop a run: this code no longer reaches that branch. Kept as its own commit
  so it can be dropped without touching the `submit()` fix above.

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
