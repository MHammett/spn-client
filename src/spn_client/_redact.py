"""Secret-redaction helpers.

Vendored from a sister project's ``ci_core.redact`` module rather than taken
as a dependency on it — this package is deliberately decoupled from any
project-specific shared library, and these two functions are the only pieces
``client.py`` needs.
"""

import re

# The sensitive word a credential parameter ends with. Hyphenated spellings are
# not hypothetical: Azure OpenAI names its parameter `api-key` and Google sends
# `X-Goog-Api-Key`. An underscore-only list matched neither, nor
# `client_secret`, `refresh_token`, `subscription-key`, `password`, `auth` or
# `sig` — eight of twelve real-world spellings tested on 2026-09-04 passed
# straight through into whatever log or report the error text reached.
_SENSITIVE_WORD = (
    r"(?:api[-_]?key|access[-_]?token|refresh[-_]?token|id[-_]?token"
    r"|client[-_]?secret|subscription[-_]?key|session[-_]?key"
    r"|key|token|secret|password|passwd|credentials?|auth|signature|sig)"
)

# Any dash/underscore-separated prefix, then that word, then `=`. Anchoring the
# end on `=` is what leaves innocent parameters alone: `author=` cannot match,
# because `auth` would have to consume the whole name and `or` is left over.
# `keywords=` survives for the same reason.
_PARAM_NAME = rf"(?:[A-Za-z0-9]+[-_])*{_SENSITIVE_WORD}"

# Matches ?key=..., &apiKey=..., &api-key=... in a URL and replaces the value.
# Stops at the next &, whitespace, quote, or closing bracket so we don't eat
# the rest of the message.
_KEY_QUERY_RE = re.compile(
    rf"([?&]{_PARAM_NAME}=)[^&\s'\"<>)\]]+",
    re.IGNORECASE,
)


def redact_url_keys(text):
    """Replace key/token query-parameter values in any string with [REDACTED].

    Provider-agnostic: works even when the key value isn't available to compare
    against, because it matches on the parameter name.
    """
    return _KEY_QUERY_RE.sub(r"\1[REDACTED]", str(text))


def redact_value(text, secret):
    """Replace a known secret value with [REDACTED] wherever it appears."""
    if secret and secret in str(text):
        return str(text).replace(secret, "[REDACTED]")
    return str(text)
