# Contributing

This is a small, single-maintainer project — I review PRs on my own schedule,
not on a support-ticket clock. That said, real contributions are genuinely
welcome; this file exists to make that easy rather than to gatekeep it.

## One thing worth knowing before you touch logic

Most of the non-obvious behavior in `client.py` exists because of a specific,
dated, measured incident against the real archive.org API — not because
someone thought it might theoretically matter. Those incidents are documented
right above the code they justify. If something in the pacing, retry, or
error-handling logic looks like unnecessary complexity, read the comment
above it first — it's very likely load-bearing, and removing it would
silently reintroduce a bug that already happened once in production.

If you're adding new behavior, the same standard applies: a code comment
should explain *why*, ideally with something concrete to point at (a
measurement, a documented archive.org behavior, a linked issue) — not just
restate what the code already says.

## Running things locally

```bash
pip install -e ".[dev]"

ruff check src tests
ruff format --check src tests
mypy src
pytest --cov=src/spn_client --cov-report=term-missing
```

All four are what CI runs on every PR — green locally means green in CI.
Tests never touch the real archive.org API (`--disable-socket` is enforced
in `pyproject.toml`); every HTTP call is mocked.

Coverage is at 98% as of this writing. A PR that drops it meaningfully should
add tests for the new code path, not just accept the drop.

## Making a PR

- Keep the existing style: plain functions, `TypedDict` return shapes,
  docstrings that document the *shape* of what's returned and, where
  relevant, *why* it's shaped that way.
- Update `CHANGELOG.md` under an `[Unreleased]` heading if your change is
  user-visible (new parameter, changed default, fixed bug). I'll fold it
  into the next version's entry when I cut a release.
- If you're changing something derived from archive.org's actual behavior
  (a rate limit, an error code, a response field), say what you observed and
  when/how — the existing code treats "measured against the live API on
  <date>" as the standard of evidence, and PRs that meet it review faster.

## Reporting a security issue

See [SECURITY.md](SECURITY.md) — please don't open a public issue for those.
