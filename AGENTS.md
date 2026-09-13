# AGENTS.md

Instructions for AI coding agents working in this repository — Claude Code,
Cursor, Codex, aider, GitHub Copilot's agent mode, or anything else editing
this repo autonomously or semi-autonomously. Read this before making changes.

Most of this repo's contribution conventions are the same regardless of who
(or what) is contributing — see [CONTRIBUTING.md](CONTRIBUTING.md) for the
full version: exact commands to run before proposing a change as finished,
the coverage bar, and PR expectations. Read that first. This file adds what's
specific to an automated agent.

## Do not release

Landing a change is not the same as shipping a release, and an agent should
never conflate the two:

- Do NOT change `version` in `pyproject.toml` or `__version__` in
  `src/spn_client/__init__.py`.
- Do NOT create a git tag, a GitHub Release, or invoke `twine upload` /
  trigger `.github/workflows/publish.yml`.
- DO add an entry under `[Unreleased]` in `CHANGELOG.md` for any
  user-visible change (new parameter, changed default, fixed bug).

The maintainer decides when accumulated `[Unreleased]` changes are tested
and coherent enough to become a real release — usually a deliberate batch
of finished, verified work, not every individual commit. If a task you've
been given seems to call for cutting a release, stop and ask rather than
doing it.

## The code comments are evidence, not clutter

Read `CONTRIBUTING.md`'s note on this carefully: most non-obvious behavior in
`client.py` is a direct response to a specific, dated, measured incident
against the real archive.org API. An agent optimizing for "cleaner code" or
"removing redundant comments" is exactly the failure mode this is warning
about — a comment that looks removable because it doesn't affect the current
tests is very likely load-bearing against a scenario the tests don't happen
to cover. If you're changing something a comment justifies, replace the
justification with your own evidence; don't just delete it.

## If you're opening a PR from outside this repo

Fork PRs run the full CI matrix (lint, format, type check, tests) with no
secrets required — everything is mocked, network calls are disabled in
tests. A green run locally (see `CONTRIBUTING.md`) should mean a green run
in CI.
