"""Tests for machine-wide rate-limit state (``use_shared_state``).

The tests in ``TestAcrossProcesses`` start real Python processes on one shared
file, because what this feature fixes is the thing a single process cannot
show: separate clocks in separate processes. Everything else is in-process
against the same file, with the wall clock patched.
"""

import itertools
import json
import subprocess
import sys
import textwrap
import time
from unittest.mock import MagicMock, patch

import pytest

from spn_client import _shared_state, client

URL = "https://example.com/page"


def _refused():
    m = MagicMock()
    m.status_code = 429
    m.url = "https://archive.org/wayback/available"
    m.headers = {}
    return m


def _answered():
    m = MagicMock()
    m.status_code = 200
    m.headers = {}
    m.json.return_value = {"archived_snapshots": {}}
    m.raise_for_status.return_value = None
    return m


@pytest.fixture
def state_file(tmp_path):
    client.reset_rate_limit_state()
    yield tmp_path / "spn_state.sqlite"
    client.reset_rate_limit_state()


@pytest.fixture
def clock(monkeypatch):
    """The shared state's wall clock, moved by hand."""

    class Clock:
        now = 1_000_000.0

        def __call__(self):
            return self.now

    c = Clock()
    monkeypatch.setattr(_shared_state, "_now", c)
    return c


def _run(code, state_file, *args):
    """Run ``code`` in a new Python process; return its stdout parsed as JSON."""
    script = textwrap.dedent(code)
    out = subprocess.run(
        [sys.executable, "-c", script, str(state_file), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


_PACER = """
    import json, sys, time
    from spn_client import client
    path, start_at, shared = sys.argv[1], float(sys.argv[2]), sys.argv[3] == "1"
    client.use_shared_state(path if shared else None)
    time.sleep(max(0.0, start_at - time.time()))
    stamps = []
    for _ in range(4):
        client._pace(0.5)
        stamps.append(time.time())
    print(json.dumps(stamps))
"""


def _pace_in_three_processes(state_file, shared):
    # Each process sleeps to one common start time, so their requests overlap
    # however long each takes to start Python.
    start_at = time.time() + 3.0
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(_PACER),
                str(state_file),
                str(start_at),
                "1" if shared else "0",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    stamps = []
    for p in procs:
        out, _ = p.communicate(timeout=120)
        assert p.returncode == 0
        stamps += json.loads(out.strip().splitlines()[-1])
    stamps.sort()
    return [b - a for a, b in itertools.pairwise(stamps)]


class TestAcrossProcesses:
    def test_without_it_processes_do_not_take_turns(self, state_file):
        """The problem, shown: three processes pacing at 0.5s each send
        requests far closer together than 0.5s."""
        gaps = _pace_in_three_processes(state_file, shared=False)
        assert min(gaps) < 0.2

    def test_processes_take_turns_on_one_clock(self, state_file):
        gaps = _pace_in_three_processes(state_file, shared=True)
        assert len(gaps) == 11
        # Slack for the sleep returning a little early or a little late.
        assert min(gaps) >= 0.45

    def test_a_breaker_tripped_in_one_process_stops_another(self, state_file):
        tripped = _run(
            """
            import json, sys
            from unittest.mock import MagicMock, patch
            from spn_client import client
            client.use_shared_state(sys.argv[1])
            refused = MagicMock(status_code=429, url="u", headers={})
            with patch("spn_client.client.requests.get", return_value=refused), \\
                 patch.object(client, "_MIN_INTERVAL_SECONDS", 0.0), \\
                 patch.object(client, "_BACKOFF_BASE_SECONDS", 0.0):
                for i in range(client._CIRCUIT_TRIP_AFTER):
                    client.check(f"https://example.com/{i}")
            print(json.dumps(client.rate_limited_out()))
            """,
            state_file,
        )
        assert tripped is True

        # This process has refused nothing itself.
        client.use_shared_state(state_file)
        assert client._rate_limited_lookups == 0
        with patch("spn_client.client.requests.get") as get:
            result = client.check(URL)
        get.assert_not_called()
        assert result["archived"] is None
        assert result["rate_limited"] is True

    def test_a_429_in_one_process_backs_off_another(self, state_file):
        _run(
            """
            import json, sys
            from spn_client import client
            client.use_shared_state(sys.argv[1])
            client._note_rate_limited(30.0)
            print(json.dumps(True))
            """,
            state_file,
        )
        shared = _shared_state.SharedState(state_file)
        assert shared.reserve(0.0) > 25.0


class TestTheBreakerClosesOnItsOwn:
    def _trip(self, shared):
        for _ in range(client._CIRCUIT_TRIP_AFTER):
            shared.note_refused(client._CIRCUIT_TRIP_AFTER, 1800.0, 300.0)

    def test_it_trips_on_refusals_from_anyone(self, state_file, clock):
        shared = _shared_state.SharedState(state_file)
        for _ in range(client._CIRCUIT_TRIP_AFTER - 1):
            shared.note_refused(client._CIRCUIT_TRIP_AFTER, 1800.0, 300.0)
        assert shared.is_open() is False
        shared.note_refused(client._CIRCUIT_TRIP_AFTER, 1800.0, 300.0)
        assert shared.is_open() is True

    def test_one_refusal_after_the_cooldown_opens_it_again(self, state_file, clock):
        shared = _shared_state.SharedState(state_file)
        self._trip(shared)
        clock.now += 301
        assert shared.is_open() is False
        shared.note_refused(client._CIRCUIT_TRIP_AFTER, 1800.0, 300.0)
        assert shared.is_open() is True

    def test_a_success_after_the_cooldown_closes_it(self, state_file, clock):
        shared = _shared_state.SharedState(state_file)
        self._trip(shared)
        clock.now += 301
        shared.note_success()
        # Closed, and the refusals that tripped it no longer count: it takes a
        # full five new ones to trip again, not one.
        for _ in range(client._CIRCUIT_TRIP_AFTER - 1):
            shared.note_refused(client._CIRCUIT_TRIP_AFTER, 1800.0, 300.0)
            clock.now += 1
        assert shared.is_open() is False

    def test_a_success_while_closed_does_not_erase_refusals(self, state_file, clock):
        shared = _shared_state.SharedState(state_file)
        for _ in range(client._CIRCUIT_TRIP_AFTER - 1):
            shared.note_refused(client._CIRCUIT_TRIP_AFTER, 1800.0, 300.0)
            shared.note_success()
        shared.note_refused(client._CIRCUIT_TRIP_AFTER, 1800.0, 300.0)
        assert shared.is_open() is True

    def test_old_refusals_stop_counting(self, state_file, clock):
        shared = _shared_state.SharedState(state_file)
        for _ in range(client._CIRCUIT_TRIP_AFTER - 1):
            shared.note_refused(client._CIRCUIT_TRIP_AFTER, 1800.0, 300.0)
        clock.now += 1801
        shared.note_refused(client._CIRCUIT_TRIP_AFTER, 1800.0, 300.0)
        assert shared.is_open() is False

    def test_a_run_start_does_not_reopen_what_another_run_tripped(self, state_file):
        client.use_shared_state(state_file)
        self._trip(client._shared)
        client.reset_rate_limit_state()
        assert client.rate_limited_out() is True


class TestTheSharedClock:
    def test_a_slot_is_reserved_after_the_last(self, state_file, clock):
        shared = _shared_state.SharedState(state_file)
        assert shared.reserve(3.0) == 0.0
        assert shared.reserve(3.0) == 3.0
        assert shared.reserve(3.0) == 6.0

    def test_a_clock_set_back_does_not_stall_every_process(self, state_file, clock):
        shared = _shared_state.SharedState(state_file)
        shared.reserve(3.0)
        shared.note_rate_limited(30.0)
        clock.now -= 86_400  # the machine's clock moved back a day
        assert shared.reserve(3.0) == 0.0


class TestTurningItOn:
    def test_the_environment_variable_turns_it_on(self, state_file, monkeypatch):
        monkeypatch.setenv(client.SHARED_STATE_ENV, str(state_file))
        client._note_rate_limited(30.0)
        assert state_file.exists()
        assert _shared_state.SharedState(state_file).reserve(0.0) > 25.0

    def test_off_by_default(self, state_file):
        client._note_rate_limited(1.0)
        assert client._shared is None
        assert not state_file.exists()

    def test_a_file_that_cannot_be_opened_is_ignored(self, tmp_path, caplog):
        # A directory where the file should be: sqlite cannot open it.
        client.use_shared_state(tmp_path)
        assert client._shared is None
        assert "could not be opened" in caplog.text
        assert client.rate_limited_out() is False

    def test_a_file_that_fails_mid_run_does_not_fail_the_call(self, state_file, caplog):
        client.use_shared_state(state_file)
        with (
            patch.object(
                _shared_state.SharedState,
                "reserve",
                side_effect=_shared_state.sqlite3.OperationalError("disk I/O error"),
            ),
            patch("spn_client.client.requests.get", return_value=_answered()) as get,
            patch.object(client, "_MIN_INTERVAL_SECONDS", 0.0),
        ):
            result = client.check(URL)
        get.assert_called_once()
        assert result["archived"] is False
        assert client._shared is None
        assert "pacing this process on its own" in caplog.text

    def test_an_answer_closes_a_half_open_breaker(self, state_file, monkeypatch):
        client.use_shared_state(state_file)
        clock = [time.time()]
        monkeypatch.setattr(_shared_state, "_now", lambda: clock[0])
        for _ in range(client._CIRCUIT_TRIP_AFTER):
            client._shared.note_refused(client._CIRCUIT_TRIP_AFTER, 1800.0, 300.0)
        assert client.rate_limited_out() is True
        clock[0] += 301
        with (
            patch("spn_client.client.requests.get", return_value=_answered()),
            patch.object(client, "_MIN_INTERVAL_SECONDS", 0.0),
        ):
            client.check(URL)
        # Closed: one refusal no longer reopens it.
        client._shared.note_refused(client._CIRCUIT_TRIP_AFTER, 1800.0, 300.0)
        assert client.rate_limited_out() is False

    def test_a_refusal_counts_toward_the_shared_breaker(self, state_file):
        client.use_shared_state(state_file)
        with (
            patch("spn_client.client.requests.get", return_value=_refused()),
            patch.object(client, "_MIN_INTERVAL_SECONDS", 0.0),
            patch.object(client, "_BACKOFF_BASE_SECONDS", 0.0),
        ):
            client.check(URL)
        client.reset_rate_limit_state()
        conn = _shared_state.sqlite3.connect(str(state_file))
        (count,) = conn.execute("SELECT count(*) FROM refusals").fetchone()
        conn.close()
        assert count == 1
