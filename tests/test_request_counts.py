"""How many requests went to archive.org, by endpoint.

Reports count outcomes per URL. Nothing counted requests, so nobody could say
how many calls one machine made to archive.org in a day (spn-client#13).
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from spn_client import _shared_state
from spn_client import client as wayback


@pytest.fixture(autouse=True)
def _fresh():
    wayback.reset_rate_limit_state()
    yield
    wayback.reset_rate_limit_state()
    wayback.use_shared_state(None)


def _resp(status=200, payload=None):
    m = MagicMock(status_code=status, headers={}, url="https://web.archive.org/x")
    m.json.return_value = payload if payload is not None else {}
    m.content = b"{}"
    if status >= 400:
        m.raise_for_status.side_effect = requests.HTTPError(str(status), response=m)
    else:
        m.raise_for_status.return_value = None
    return m


def _get(url):
    with (
        patch.object(wayback.time, "sleep"),
        patch("spn_client.client.requests.get", return_value=_resp()),
    ):
        wayback.paced_get(url, timeout=5)


class TestThisRun:
    def test_nothing_sent_is_an_empty_count(self):
        assert wayback.request_counts() == {}

    @pytest.mark.parametrize(
        "url, label",
        [
            (wayback._AVAILABILITY_API, "availability"),
            (wayback._CDX_API, "cdx"),
            (wayback._SAVE_STATUS_API + "/spn2-abc", "job_status"),
            (wayback._SAVE_USER_STATUS_API, "user_status"),
            (wayback._SAVE_SYSTEM_STATUS_API, "system_status"),
            ("https://web.archive.org/web/2026/https://example.com/", "caller"),
        ],
    )
    def test_each_endpoint_has_its_own_count(self, url, label):
        _get(url)
        assert wayback.request_counts() == {label: 1}

    def test_a_submission_counts_as_save(self):
        with (
            patch.object(wayback.time, "sleep"),
            patch("spn_client.client.requests.get", return_value=_resp(302)),
        ):
            wayback.submit("https://example.com/")
        assert wayback.request_counts().get("save") == 1

    def test_every_attempt_counts_not_every_call(self):
        # Three 429s are three requests archive.org received.
        with (
            patch.object(wayback.time, "sleep"),
            patch("spn_client.client.requests.get", return_value=_resp(429)),
            pytest.raises(requests.HTTPError),
        ):
            wayback.paced_get(wayback._AVAILABILITY_API, timeout=5)
        assert wayback.request_counts() == {"availability": wayback._MAX_ATTEMPTS}

    def test_a_request_the_caller_makes_itself_counts_at_pace(self):
        with patch.object(wayback.time, "sleep"):
            wayback.pace()
            wayback.pace()
        assert wayback.request_counts() == {"caller": 2}

    def test_a_call_the_breaker_skipped_sent_nothing(self):
        with patch.object(wayback, "rate_limited_out", return_value=True):
            wayback.check("https://example.com/")
        assert wayback.request_counts() == {}

    def test_the_result_is_a_copy(self):
        _get(wayback._CDX_API)
        wayback.request_counts()["cdx"] = 99
        assert wayback.request_counts() == {"cdx": 1}

    def test_reset_starts_the_run_at_zero(self):
        _get(wayback._CDX_API)
        wayback.reset_rate_limit_state()
        assert wayback.request_counts() == {}


class TestTheWholeMachine:
    def test_without_shared_state_there_is_no_machine_count(self):
        _get(wayback._CDX_API)
        assert wayback.request_counts(machine_wide=True) == {}

    def test_every_process_adds_to_one_days_count(self, tmp_path):
        path = tmp_path / "s.sqlite"
        wayback.use_shared_state(path)
        _get(wayback._CDX_API)
        # Another process: its own SharedState on the same file.
        _shared_state.SharedState(path).count_request("cdx")
        _shared_state.SharedState(path).count_request("save")
        assert wayback.request_counts(machine_wide=True) == {"cdx": 2, "save": 1}

    def test_a_reset_does_not_erase_the_machines_count(self, tmp_path):
        wayback.use_shared_state(tmp_path / "s.sqlite")
        _get(wayback._CDX_API)
        wayback.reset_rate_limit_state()
        assert wayback.request_counts() == {}
        assert wayback.request_counts(machine_wide=True) == {"cdx": 1}

    def test_days_are_kept_apart_and_old_ones_dropped(self, tmp_path, monkeypatch):
        shared = _shared_state.SharedState(tmp_path / "s.sqlite")
        clock = [1_800_000_000.0]
        monkeypatch.setattr(_shared_state, "_now", lambda: clock[0])
        shared.count_request("cdx")
        first_day = _shared_state._day(clock[0])
        clock[0] += 86400
        shared.count_request("cdx")
        shared.count_request("cdx")
        assert shared.request_counts(first_day) == {"cdx": 1}
        assert shared.request_counts() == {"cdx": 2}
        clock[0] += 86400 * (_shared_state.COUNT_RETENTION_DAYS + 1)
        shared.count_request("save")
        assert shared.request_counts(first_day) == {}

    def test_a_file_made_by_0_5_0_gains_the_table(self, tmp_path):
        import sqlite3

        path = tmp_path / "old.sqlite"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE clock (id INTEGER PRIMARY KEY CHECK (id = 1),"
            " last_call_at REAL NOT NULL, blocked_until REAL NOT NULL,"
            " open_until REAL NOT NULL, half_open INTEGER NOT NULL,"
            " closed_at REAL NOT NULL);"
            "INSERT INTO clock VALUES (1, 0, 0, 0, 0, 0);"
            "CREATE TABLE refusals (at REAL NOT NULL);"
        )
        conn.close()
        shared = _shared_state.SharedState(path)
        shared.count_request("cdx")
        assert shared.request_counts() == {"cdx": 1}
