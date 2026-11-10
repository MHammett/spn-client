"""Rate-limit state shared by every process on one machine, in a SQLite file.

``client``'s pacing clock, backoff and circuit breaker are process-wide, but
archive.org throttles per IP. N processes on one machine each pacing at
``_MIN_INTERVAL_SECONDS`` send N requests per interval, and each has to collect
its own refusals before its breaker trips. Seen 2026-10-29 in
content-intelligence: four review runs on one machine inside 45 minutes, each
with its own clock; one run's breaker tripped twice, and a probe after they had
all ended got a 429 on its first request.

SQLite, because its file locking already works across processes on Windows,
macOS and Linux and it is in the standard library (this package's only
dependency is ``requests``). A general rate limiter (pyrate-limiter, which also
offers a SQLite bucket) models "N calls per window"; what has to be shared here
is ``client``'s own model — a minimum gap, a backoff a 429 imposes on everyone,
and a breaker — so it is written against that model directly.

``BEGIN IMMEDIATE`` takes the write lock, which makes each read-modify-write
atomic across processes. The lock is held for that update only, never for the
wait: a caller reserves the next free slot on the shared clock and sleeps
outside the lock, so processes queue in slot order instead of contending.

Times are wall-clock (``time.time()``): ``time.monotonic()`` is per process.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from collections.abc import Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clock (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    last_call_at  REAL NOT NULL,
    blocked_until REAL NOT NULL,
    open_until    REAL NOT NULL,
    half_open     INTEGER NOT NULL,
    closed_at     REAL NOT NULL
);
INSERT OR IGNORE INTO clock VALUES (1, 0, 0, 0, 0, 0);
CREATE TABLE IF NOT EXISTS refusals (at REAL NOT NULL);
"""

#: A reserved slot further ahead than this is not a queue, it is a clock that
#: moved: another process wrote its times before this machine's clock was set
#: back. The longest legitimate wait is a few processes queued behind a 60s
#: Retry-After, so ten minutes is far outside it.
MAX_PLAUSIBLE_WAIT_SECONDS = 600.0


def _now() -> float:
    return time.time()


class SharedState:
    """One SQLite file holding the shared clock, backoff and breaker.

    Every method opens its own connection: the file is touched at most once
    per request to archive.org (seconds apart), and a fresh connection per
    call is safe across threads and processes without further bookkeeping.
    Raises ``sqlite3.Error`` or ``OSError``; the caller decides what a broken
    file means.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Every statement in the schema is idempotent, so processes that
        # create the file at the same moment cannot disagree about it.
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            # Nothing here needs to survive a power cut: a lost write is a
            # clock a few seconds out, which the next request corrects. Without
            # this every request waits on an fsync of the journal.
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @staticmethod
    def _row(conn: sqlite3.Connection) -> tuple[float, float, float, int, float]:
        row = conn.execute(
            "SELECT last_call_at, blocked_until, open_until, half_open, closed_at "
            "FROM clock WHERE id = 1"
        ).fetchone()
        return row  # type: ignore[no-any-return]

    def reserve(self, min_interval: float) -> float:
        """Claim the next slot at least ``min_interval`` after the last one and
        after any backoff; return the seconds to wait for it."""
        with self._write() as conn:
            last, blocked, _, _, _ = self._row(conn)
            now = _now()
            ceiling = now + MAX_PLAUSIBLE_WAIT_SECONDS
            if last > ceiling:
                last = 0.0
            if blocked > ceiling:
                blocked = 0.0
            slot = max(now, last + min_interval, blocked)
            conn.execute("UPDATE clock SET last_call_at = ? WHERE id = 1", (slot,))
        # Measured after the commit: the slot is a time, and the commit and
        # close took some of the wait already.
        return max(0.0, slot - _now())

    def note_rate_limited(self, retry_after: float) -> None:
        """Back off every process on the machine, not only this one."""
        with self._write() as conn:
            conn.execute(
                "UPDATE clock SET blocked_until = max(blocked_until, ?) WHERE id = 1",
                (_now() + retry_after,),
            )

    def note_refused(self, trip_after: int, window: float, cooldown: float) -> None:
        """Count one refused lookup; trip the breaker at ``trip_after`` refusals
        inside ``window`` seconds, for ``cooldown`` seconds.

        After the cooldown the breaker is half-open: the next request goes out,
        and if it is refused too the breaker opens again at once, without
        waiting for ``trip_after`` more. A success closes it (``note_success``).
        """
        with self._write() as conn:
            _, _, open_until, half_open, closed_at = self._row(conn)
            now = _now()
            if half_open and open_until <= now:
                conn.execute(
                    "UPDATE clock SET open_until = ? WHERE id = 1", (now + cooldown,)
                )
                return
            conn.execute("DELETE FROM refusals WHERE at < ?", (now - window,))
            conn.execute("INSERT INTO refusals VALUES (?)", (now,))
            (count,) = conn.execute(
                "SELECT count(*) FROM refusals WHERE at > ?",
                (max(now - window, closed_at),),
            ).fetchone()
            if count >= trip_after and open_until <= now:
                conn.execute(
                    "UPDATE clock SET open_until = ?, half_open = 1 WHERE id = 1",
                    (now + cooldown,),
                )

    def note_success(self) -> None:
        """Close a half-open breaker. Refusals before now stop counting.

        A success while the breaker is closed changes nothing: one process's
        200 must not erase other processes' refusals (``client``'s note on why
        "consecutive" cannot work across interleaved callers).
        """
        with self._write() as conn:
            _, _, open_until, half_open, _ = self._row(conn)
            now = _now()
            if half_open and open_until <= now:
                conn.execute(
                    "UPDATE clock SET half_open = 0, closed_at = ? WHERE id = 1",
                    (now,),
                )

    def is_open(self) -> bool:
        """True while the breaker is open (not while it is half-open)."""
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            (open_until,) = conn.execute(
                "SELECT open_until FROM clock WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        return bool(open_until > _now())
