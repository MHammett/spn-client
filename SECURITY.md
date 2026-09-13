# Security

## Reporting a vulnerability

Please report security issues privately via [GitHub's private vulnerability
reporting](https://github.com/MHammett/spn-client/security/advisories/new)
rather than a public issue. You should get an acknowledgement within a few
days.

## Scope notes specific to this library

- This client handles S3-style access/secret key pairs and, optionally, a
  target-page login password/cookie, passed to it by the caller. It never
  stores or transmits them anywhere except in the `Authorization`/form
  fields of the specific archive.org request they belong to, and redacts
  them from error text and logs (see `_redact.py`).
- It makes outbound HTTP requests only to `archive.org`/`web.archive.org`
  endpoints named in `client.py` — no telemetry, no other third parties.
- If you find a case where a secret leaks into an exception message, a log
  line, or a returned dict where it shouldn't, that's a security bug —
  please report it privately as above rather than filing a public issue.
