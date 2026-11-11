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
