"""Outbound-HTTP identity for spn-client.

A dedicated User-Agent, separate from any consuming project's own — this
package is meant to be embedded in several unrelated projects, and its
requests to archive.org should identify the library making them, not whatever
application happens to import it.
"""

from importlib import metadata

try:
    _VERSION = metadata.version("spn-client")
except metadata.PackageNotFoundError:  # pragma: no cover - source/dev checkout
    _VERSION = "0.0.0"

USER_AGENT: str = f"spn-client/{_VERSION}"

DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
