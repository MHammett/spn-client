"""Public pacing for a caller's own archive.org requests.

A consumer that fetches archive.org itself (a snapshot read, say) would otherwise
sit outside the module's clock and breaker, and add to the same per-IP budget
unpaced. ``pace()`` and ``paced_get()`` put those requests on the same clock.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from spn_client import client as wayback

SNAPSHOT = "https://web.archive.org/web/20260905121627/https://example.com/"


@pytest.fixture(autouse=True)
def _fresh_clock():
    wayback.reset_rate_limit_state()
    yield
    wayback.reset_rate_limit_state()


def _ok():
    m = MagicMock(status_code=200, headers={}, url=SNAPSHOT)
    m.raise_for_status.return_value = None
    return m


def _status(code):
    m = MagicMock(status_code=code, headers={}, url=SNAPSHOT)
    m.raise_for_status.side_effect = requests.HTTPError(
        f"{code} Client Error", response=m
    )
    return m


def test_pace_spaces_a_callers_requests_by_the_minimum_interval():
    slept = []
    with patch.object(wayback.time, "sleep", side_effect=slept.append):
        wayback.pace()
        wayback.pace()
    assert len(slept) == 1
    assert slept[0] == pytest.approx(wayback._MIN_INTERVAL_SECONDS, abs=0.5)


def test_paced_get_returns_the_response_and_is_paced():
    with (
        patch.object(wayback.time, "sleep") as sleep,
        patch("spn_client.client.requests.get", return_value=_ok()) as get,
    ):
        resp = wayback.paced_get(SNAPSHOT, timeout=5)
        wayback.paced_get(SNAPSHOT, timeout=5)
    assert resp.status_code == 200
    assert get.call_count == 2
    assert sleep.called  # the second call waited on the shared clock


def test_a_429_on_a_callers_fetch_backs_off_everyone():
    with (
        patch.object(wayback.time, "sleep"),
        patch("spn_client.client.requests.get", return_value=_status(429)),
        patch.object(wayback, "_note_rate_limited") as note,
        pytest.raises(requests.HTTPError),
    ):
        wayback.paced_get(SNAPSHOT, timeout=5)
    assert note.called


def test_a_callers_429s_count_toward_the_shared_breaker():
    with (
        patch.object(wayback.time, "sleep"),
        patch("spn_client.client.requests.get", return_value=_status(429)),
    ):
        for _ in range(wayback._CIRCUIT_TRIP_AFTER):
            with pytest.raises(requests.HTTPError):
                wayback.paced_get(SNAPSHOT, timeout=5)
    assert wayback.rate_limited_out() is True


def _refused():
    return requests.exceptions.ConnectionError(ConnectionRefusedError(10061, "refused"))


class TestNoteOutcome:
    """A caller that must use its own fetcher (an SSRF guard, say) paces with
    ``pace()`` and then says what archive.org answered."""

    def test_a_429_backs_off_and_counts(self):
        with patch.object(wayback, "_note_rate_limited") as note:
            for _ in range(wayback._CIRCUIT_TRIP_AFTER):
                wayback.note_outcome(status_code=429)
        assert note.call_count == wayback._CIRCUIT_TRIP_AFTER
        assert wayback.rate_limited_out() is True

    def test_a_429_honours_the_callers_retry_after(self):
        with patch.object(wayback, "_note_rate_limited") as note:
            wayback.note_outcome(status_code=429, retry_after=42.0)
        note.assert_called_once_with(42.0)

    def test_a_refused_connection_counts_as_one(self):
        with patch.object(wayback.time, "sleep"):
            for _ in range(wayback._REFUSED_TRIP_AFTER):
                wayback.note_outcome(exc=_refused())
        assert wayback.rate_limited_out() is True

    @pytest.mark.parametrize(
        "exc",
        [requests.exceptions.ReadTimeout("slow"), ValueError("not transport")],
    )
    def test_other_failures_count_for_nothing(self, exc):
        for _ in range(10):
            wayback.note_outcome(exc=exc)
        assert wayback.rate_limited_out() is False

    def test_an_answer_counts_for_nothing(self):
        for code in (200, 404, 503) * 5:
            wayback.note_outcome(status_code=code)
        assert wayback.rate_limited_out() is False


class TestBreakerReason:
    def test_closed_is_none(self):
        assert wayback.breaker_reason() is None

    def test_429s_say_rate_limited(self):
        with patch.object(wayback, "_note_rate_limited"):
            for _ in range(wayback._CIRCUIT_TRIP_AFTER):
                wayback.note_outcome(status_code=429)
        assert wayback.breaker_reason() == "rate_limited"

    def test_refusals_say_connection_refused(self):
        for _ in range(wayback._REFUSED_TRIP_AFTER):
            wayback.note_outcome(exc=_refused())
        assert wayback.breaker_reason() == "connection_refused"

    def test_another_processes_trip_says_machine_wide(self, tmp_path):
        wayback.use_shared_state(tmp_path / "s.sqlite")
        try:
            for _ in range(wayback._CIRCUIT_TRIP_AFTER):
                wayback._shared.note_refused(wayback._CIRCUIT_TRIP_AFTER, 1800.0, 300.0)
            assert wayback.breaker_reason() == "machine_wide"
        finally:
            wayback.use_shared_state(None)


class TestDefaultSharedStatePath:
    def test_windows_uses_localappdata(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wayback.os, "name", "nt")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        path = wayback.default_shared_state_path()
        assert path.startswith(str(tmp_path))
        assert path.endswith("rate-limit.sqlite")
        assert "spn-client" in path

    def test_elsewhere_uses_the_xdg_cache(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wayback.os, "name", "posix")
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        assert wayback.default_shared_state_path().startswith(str(tmp_path))

    def test_it_is_the_same_for_every_caller(self):
        assert (
            wayback.default_shared_state_path() == wayback.default_shared_state_path()
        )

    def test_nothing_is_created_by_asking(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wayback.os, "name", "nt")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        wayback.default_shared_state_path()
        assert list(tmp_path.iterdir()) == []
