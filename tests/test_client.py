"""Tests for spn_client.client (all HTTP mocked)."""

import itertools
import threading
import time
from unittest.mock import patch, MagicMock

import requests

from spn_client import client as wayback

JOB_ID = "spn2-abc123"


def _mock_response(payload, url="https://example.com"):
    """A 200. ``url`` is the *final* URL after redirects, which is load-bearing
    for submissions: an unauthenticated Save Page Now lands on the snapshot it
    just wrote, and that redirect is the only place the snapshot URL appears.
    Defaults to a non-archive URL, i.e. "no snapshot established"."""
    m = MagicMock()
    m.json.return_value = payload
    m.raise_for_status.return_value = None
    m.url = url
    return m


class TestWaybackCheck:
    def test_archived_url(self):
        payload = {
            "archived_snapshots": {
                "closest": {
                    "available": True,
                    "url": "https://web.archive.org/web/20250101000000/https://example.com",
                    "timestamp": "20250101000000",
                }
            }
        }
        with patch(
            "spn_client.client.requests.get",
            return_value=_mock_response(payload),
        ):
            result = wayback.check("https://example.com")
        assert result["archived"] is True
        assert result["snapshot_url"].startswith("https://web.archive.org")
        assert result["snapshot_age_days"] is not None
        assert isinstance(result["snapshot_stale"], bool)

    def test_not_archived(self):
        payload = {"archived_snapshots": {}}
        with patch(
            "spn_client.client.requests.get",
            return_value=_mock_response(payload),
        ):
            result = wayback.check("https://example.com")
        assert result["archived"] is False
        assert "snapshot_url" not in result

    def test_network_error_returns_none_archived(self):
        with patch(
            "spn_client.client.requests.get",
            side_effect=Exception("timeout"),
        ):
            result = wayback.check("https://example.com")
        assert result["archived"] is None
        assert "error" in result

    def test_stale_snapshot_flagged(self):
        # Snapshot from 2020 should be stale (>180 days)
        payload = {
            "archived_snapshots": {
                "closest": {
                    "available": True,
                    "url": "https://web.archive.org/web/20200101000000/https://old.com",
                    "timestamp": "20200101000000",
                }
            }
        }
        with patch(
            "spn_client.client.requests.get",
            return_value=_mock_response(payload),
        ):
            result = wayback.check("https://old.com")
        assert result["snapshot_stale"] is True

    def test_fresh_snapshot_not_stale(self):
        from datetime import datetime, timezone, timedelta

        recent_ts = (datetime.now(timezone.utc) - timedelta(days=10)).strftime(
            "%Y%m%d"
        ) + "000000"
        payload = {
            "archived_snapshots": {
                "closest": {
                    "available": True,
                    "url": f"https://web.archive.org/web/{recent_ts}/https://fresh.com",
                    "timestamp": recent_ts,
                }
            }
        }
        with patch(
            "spn_client.client.requests.get",
            return_value=_mock_response(payload),
        ):
            result = wayback.check("https://fresh.com")
        assert result["snapshot_stale"] is False

    def test_custom_stale_days_override(self):
        from datetime import datetime, timezone, timedelta

        # Snapshot from 100 days ago: NOT stale at a lenient 180, but stale at
        # a stricter 90. Both passed explicitly rather than relying on
        # DEFAULT_STALE_DAYS, which is a library fallback, not a policy any
        # test should depend on the exact value of.
        ts = (datetime.now(timezone.utc) - timedelta(days=100)).strftime(
            "%Y%m%d"
        ) + "000000"
        payload = {
            "archived_snapshots": {
                "closest": {
                    "available": True,
                    "url": f"https://web.archive.org/web/{ts}/https://x.com",
                    "timestamp": ts,
                }
            }
        }
        with patch(
            "spn_client.client.requests.get",
            return_value=_mock_response(payload),
        ):
            lenient_result = wayback.check("https://x.com", stale_days=180)
            strict_result = wayback.check("https://x.com", stale_days=90)
        assert lenient_result["snapshot_stale"] is False
        assert strict_result["snapshot_stale"] is True

    def test_archive_url_detected_without_api_call(self):
        # A web.archive.org link is recognized from its embedded timestamp — no
        # archive-of-an-archive lookup. requests.get must not be called.
        from datetime import datetime, timezone, timedelta

        recent = (datetime.now(timezone.utc) - timedelta(days=20)).strftime(
            "%Y%m%d"
        ) + "120000"
        url = f"https://web.archive.org/web/{recent}/https://example.com/article"
        with patch(
            "spn_client.client.requests.get"
        ) as mock_get:
            result = wayback.check(url)
        mock_get.assert_not_called()
        assert result["is_archive_url"] is True
        assert result["archived"] is True
        assert result["snapshot_age_days"] == 20
        assert result["snapshot_stale"] is False

    def test_archive_url_stale_flagged(self):
        url = "https://web.archive.org/web/20200101000000/https://old.example.com"
        result = wayback.check(url)
        assert result["is_archive_url"] is True
        assert result["snapshot_stale"] is True

    def test_archive_url_respects_custom_stale_days(self):
        from datetime import datetime, timezone, timedelta

        ts = (datetime.now(timezone.utc) - timedelta(days=100)).strftime(
            "%Y%m%d"
        ) + "000000"
        url = f"https://web.archive.org/web/{ts}/https://example.com"
        # Explicit on both sides rather than relying on DEFAULT_STALE_DAYS —
        # the default is a library fallback, not a policy any test should
        # depend on the exact value of.
        assert wayback.check(url, stale_days=180)["snapshot_stale"] is False
        assert wayback.check(url, stale_days=90)["snapshot_stale"] is True

    def test_non_archive_url_still_queries_api(self):
        # Regression: ordinary URLs must still hit the availability API.
        payload = {"archived_snapshots": {}}
        with patch(
            "spn_client.client.requests.get",
            return_value=_mock_response(payload),
        ) as mock_get:
            wayback.check("https://example.com/normal")
        mock_get.assert_called_once()


class TestWaybackSubmit:
    def test_unauthenticated_submit_success(self):
        resp = _mock_response({})
        with patch(
            "spn_client.client.requests.get",
            return_value=resp,
        ) as mock_get:
            result = wayback.submit("https://example.com")
        assert result["submitted"] is True
        assert result["job_id"] is None
        # No credentials given — falls back to the unauthenticated trigger endpoint.
        mock_get.assert_called_once()
        assert (
            "https://web.archive.org/save/https://example.com"
            in mock_get.call_args[0][0]
        )

    def test_authenticated_submit_success(self):
        resp = _mock_response({"job_id": "spn2-abc123"})
        with patch(
            "spn_client.client.requests.post",
            return_value=resp,
        ) as mock_post:
            result = wayback.submit(
                "https://example.com",
                access_key="AK123",
                secret_key="SK456",
            )
        assert result["submitted"] is True
        assert result["job_id"] == "spn2-abc123"
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "LOW AK123:SK456"

    def test_submit_failure_does_not_raise(self):
        with patch(
            "spn_client.client.requests.get",
            side_effect=Exception("rate limited"),
        ):
            result = wayback.submit("https://example.com")
        assert result["submitted"] is False
        assert "rate limited" in result["error"]

    def test_submit_authenticated_failure_redacts_secret(self):
        with patch(
            "spn_client.client.requests.post",
            side_effect=Exception("auth failed for SK456"),
        ):
            result = wayback.submit(
                "https://example.com",
                access_key="AK123",
                secret_key="SK456",
            )
        assert result["submitted"] is False
        assert "SK456" not in result["error"]


def _rate_limited_response(retry_after="0"):
    """A 429. ``Retry-After: 0`` keeps the shared clock from actually sleeping,
    which is what makes these tests fast; the one test that cares about the
    clock moving passes a real value."""
    m = MagicMock()
    m.status_code = 429
    m.url = "https://archive.org/wayback/available"
    m.headers = {"Retry-After": retry_after}
    return m


def _ok_response():
    m = MagicMock()
    m.status_code = 200
    m.headers = {}
    m.json.return_value = {"archived_snapshots": {}}
    m.raise_for_status.return_value = None
    return m


class TestRateLimitGuard:
    """The guard is process-wide state driven from a resolver thread pool.

    Every test here resets that state first, because it outlives a single test
    exactly the way it outlives a single run — which is the bug the reset in
    ``run_draft_pipeline`` exists to prevent.
    """

    def setup_method(self):
        wayback.reset_rate_limit_state()

    def teardown_method(self):
        wayback.reset_rate_limit_state()

    def test_breaker_trips_when_most_lookups_are_refused(self):
        """A success must not erase other threads' refusals.

        Eight workers against an endpoint refusing four of every five: the run
        is plainly being throttled and the breaker has to trip. Resetting a
        shared counter on success made this run forever, because some thread
        was always succeeding and zeroing the count.
        """
        import concurrent.futures

        counter = itertools.count()
        lock = threading.Lock()

        def flaky_get(*args, **kwargs):
            with lock:
                n = next(counter)
            return _ok_response() if n % 5 == 4 else _rate_limited_response()

        with (
            patch(
                "spn_client.client.requests.get",
                side_effect=flaky_get,
            ),
            patch.object(wayback, "_MIN_INTERVAL_SECONDS", 0.0),
            patch.object(wayback, "_BACKOFF_BASE_SECONDS", 0.0),
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(
                    pool.map(
                        lambda i: wayback.check(f"https://example.com/{i}"),
                        range(24),
                    )
                )

        assert wayback.rate_limited_out() is True

    def test_a_refused_lookup_counts_once_not_once_per_attempt(self):
        """``_CIRCUIT_TRIP_AFTER`` counts lookups, so it must mean lookups.

        Counting each retry inside a lookup tripped the breaker after two URLs
        while the constant said five.
        """
        with (
            patch(
                "spn_client.client.requests.get",
                return_value=_rate_limited_response(),
            ),
            patch.object(wayback, "_MIN_INTERVAL_SECONDS", 0.0),
            patch.object(wayback, "_BACKOFF_BASE_SECONDS", 0.0),
        ):
            wayback.check("https://example.com/one")
            assert wayback._rate_limited_lookups == 1
            assert wayback.rate_limited_out() is False

            for i in range(wayback._CIRCUIT_TRIP_AFTER - 1):
                wayback.check(f"https://example.com/{i}")

        assert wayback._rate_limited_lookups == wayback._CIRCUIT_TRIP_AFTER
        assert wayback.rate_limited_out() is True

    def test_a_429_backs_off_every_thread_not_just_the_one_that_hit_it(self):
        """The backoff has to land on shared state, or it is not a backoff.

        Sleeping inside the failing thread left the other workers calling at
        full pace, so the process never actually slowed down.
        """
        with (
            patch(
                "spn_client.client.requests.get",
                return_value=_rate_limited_response(retry_after="30"),
            ),
            patch.object(wayback, "_MIN_INTERVAL_SECONDS", 0.0),
            patch.object(wayback, "_BACKOFF_BASE_SECONDS", 0.0),
        ):
            before = time.monotonic()
            # One attempt is enough to prove the clock moved; the retries would
            # each sleep out the 30s backoff this test is asserting exists.
            with patch.object(wayback, "_MAX_ATTEMPTS", 1):
                wayback.check("https://example.com/blocked")

        # Retry-After: 30 pushes the shared clock into the future for everyone,
        # not just for the thread that collected the 429.
        assert wayback._blocked_until >= before + 30.0

    def test_reset_clears_a_tripped_breaker(self):
        """Without this, run N+1 in the same process skips every lookup."""
        with (
            patch(
                "spn_client.client.requests.get",
                return_value=_rate_limited_response(),
            ),
            patch.object(wayback, "_MIN_INTERVAL_SECONDS", 0.0),
            patch.object(wayback, "_BACKOFF_BASE_SECONDS", 0.0),
        ):
            for i in range(wayback._CIRCUIT_TRIP_AFTER):
                wayback.check(f"https://example.com/{i}")

        assert wayback.rate_limited_out() is True
        wayback.reset_rate_limit_state()
        assert wayback.rate_limited_out() is False
        assert wayback._blocked_until == 0.0

    def test_a_success_still_returns_normally(self):
        """The budget must not break the ordinary path."""
        with (
            patch(
                "spn_client.client.requests.get",
                return_value=_ok_response(),
            ),
            patch.object(wayback, "_MIN_INTERVAL_SECONDS", 0.0),
        ):
            result = wayback.check("https://example.com")

        assert result["archived"] is False
        assert wayback.rate_limited_out() is False


class TestNonJsonAvailabilityResponse:
    def setup_method(self):
        wayback.reset_rate_limit_state()

    def teardown_method(self):
        wayback.reset_rate_limit_state()

    def test_non_json_200_is_not_reported_as_a_parse_error(self):
        """Say what arrived, not what the JSON decoder thought of it.

        The raw decoder message sends the reader after a parser bug that isn't
        there — the misdirection reported upstream as akamhy/waybackpy#200.
        """
        m = MagicMock()
        m.status_code = 200
        m.headers = {}
        m.content = b"<html><body>archive.org is temporarily unavailable</body></html>"
        m.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
        m.raise_for_status.return_value = None

        with (
            patch(
                "spn_client.client.requests.get",
                return_value=m,
            ),
            patch.object(wayback, "_MIN_INTERVAL_SECONDS", 0.0),
        ):
            result = wayback.check("https://example.com")

        assert result["archived"] is None
        assert "non-JSON 200 response" in result["error"]
        assert "Expecting value" not in result["error"]


class TestPacingClock:
    """``_pace()`` actually waits on the shared clock.

    Everything else about the guard is asserted in ``TestRateLimitGuard``, but
    every test there — and the ``neutralise_wayback_pacing`` fixture in
    ``conftest.py`` — sets the intervals to 0.0, so nothing checks that a
    non-zero interval is honoured. Before this class, the only thing standing
    behind ``_MIN_INTERVAL_SECONDS`` was six unrelated tests in
    ``TestWaybackCheck`` incidentally sitting out the wait and asserting nothing
    about it.

    The clock is left real and ``time.sleep`` is recorded instead of served, so
    these assert the duration ``_pace`` computes without spending it.
    """

    def setup_method(self):
        wayback.reset_rate_limit_state()

    def teardown_method(self):
        wayback.reset_rate_limit_state()

    def test_a_second_call_waits_out_the_minimum_interval(self):
        slept = []
        with (
            patch.object(wayback, "_MIN_INTERVAL_SECONDS", 3.0),
            patch.object(wayback.time, "sleep", side_effect=slept.append),
        ):
            wayback._pace()
            assert slept == [], "The first call of a run has nothing to wait for"
            wayback._pace()

        assert len(slept) == 1, f"Second call did not pace at all: {slept}"
        # Back-to-back, so the wait is the whole interval bar the microseconds
        # spent between the two calls.
        assert 2.9 <= slept[0] <= 3.0, slept

    def test_a_429_from_another_thread_holds_this_one_back(self):
        """The half that makes the backoff process-wide.

        ``TestRateLimitGuard`` proves a 429 pushes ``_blocked_until`` forward;
        this proves a caller that never saw the 429 waits for it. Without both,
        the shared clock could move and be ignored.
        """
        slept = []
        with (
            patch.object(wayback, "_MIN_INTERVAL_SECONDS", 0.0),
            patch.object(wayback.time, "sleep", side_effect=slept.append),
        ):
            wayback._note_rate_limited(30.0)
            wayback._pace()

        assert len(slept) == 1, "A backed-off caller did not wait at all"
        assert 29.0 <= slept[0] <= 30.0, slept

    def test_the_later_of_the_two_deadlines_wins(self):
        """A short interval must not let a caller through a long backoff."""
        slept = []
        with (
            patch.object(wayback, "_MIN_INTERVAL_SECONDS", 3.0),
            patch.object(wayback.time, "sleep", side_effect=slept.append),
        ):
            wayback._note_rate_limited(30.0)
            wayback._pace()

        assert len(slept) == 1, "A backed-off caller did not wait at all"
        assert 29.0 <= slept[0] <= 30.0, (
            f"Waited {slept} — the 3s interval won over the 30s backoff"
        )


def _json_response(payload, status_code=200, content=b"{}"):
    """A 200 carrying JSON, as the job-status endpoint returns."""
    m = MagicMock()
    m.status_code = status_code
    m.headers = {}
    m.content = content
    m.json.return_value = payload
    m.raise_for_status.return_value = None
    return m


def _error_response(status_code):
    """A response whose ``raise_for_status`` raises, carrying itself as
    ``.response`` the way ``requests`` does."""
    m = MagicMock()
    m.status_code = status_code
    m.headers = {}
    m.raise_for_status.side_effect = requests.HTTPError(
        f"{status_code} Client Error", response=m
    )
    return m


class TestSubmitEstablishesWhatItCan:
    """A submission that returns a snapshot URL is not the same fact as one that
    returns nothing, and ``submitted: True`` was recording them identically."""

    ARCHIVE = "https://web.archive.org/web/20260905121627/https://example.com/"

    def test_the_redirect_target_is_kept_as_the_snapshot(self):
        """The unauthenticated endpoint captures inline and 302s to the result.

        Measured against the live service 2026-09-05: one hop from
        ``/save/<url>`` to a ``/web/<ts>/<url>`` minted seconds earlier. The old
        code followed that redirect and discarded ``resp.url``, so the pipeline
        threw away a finished answer and told the author to wait a run for it.
        """
        with patch(
            "spn_client.client.requests.get",
            return_value=_mock_response({}, url=self.ARCHIVE),
        ):
            result = wayback.submit("https://example.com/")

        assert result["submitted"] is True
        assert result["archived"] is True
        assert result["snapshot_url"] == self.ARCHIVE
        assert result["snapshot_ts"] == "20260905121627"

    def test_a_submission_that_names_no_snapshot_does_not_claim_one(self):
        """No redirect to a snapshot means nothing was established."""
        with patch(
            "spn_client.client.requests.get",
            return_value=_mock_response({}, url="https://web.archive.org/save/x"),
        ):
            result = wayback.submit("https://example.com/")

        assert result["submitted"] is True
        assert result["archived"] is False
        assert "snapshot_url" not in result

    def test_freshness_is_measured_not_assumed(self):
        """A save can redirect to a pre-existing snapshot rather than a new one.

        Stamping "captured just now" on whatever the redirect lands on would
        turn a four-year-old copy into a fresh one in the report.
        """
        old = "https://web.archive.org/web/20200101000000/https://example.com/"
        with patch(
            "spn_client.client.requests.get",
            return_value=_mock_response({}, url=old),
        ):
            result = wayback.submit("https://example.com/")

        assert result["archived"] is True
        assert result["snapshot_age_days"] > 180
        assert result["snapshot_stale"] is True

    def test_an_authenticated_submission_returns_a_job_not_a_snapshot(self):
        with patch(
            "spn_client.client.requests.post",
            return_value=_mock_response({"job_id": "spn2-abc123"}),
        ):
            result = wayback.submit(
                "https://example.com/", access_key="AK", secret_key="SK"
            )

        assert result["job_id"] == "spn2-abc123"
        assert result["archived"] is False

    def test_a_read_timeout_is_not_reported_as_a_refusal(self):
        """Observed live 2026-09-05: a 30s read timeout on a save archive.org
        had almost certainly accepted. We stopped listening; that is not the
        same fact as archive.org saying no, and reporting it as one is the same
        overstatement as calling a submission "archived"."""
        with patch(
            "spn_client.client.requests.get",
            side_effect=requests.exceptions.ReadTimeout("Read timed out."),
        ):
            result = wayback.submit("https://example.com/")
        assert result["submitted"] is False
        assert result["outcome_unknown"] is True

    def test_a_connection_refusal_is_a_refusal(self):
        """The other side of the same line: this one really did fail."""
        with patch(
            "spn_client.client.requests.get",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            result = wayback.submit("https://example.com/")
        assert result["submitted"] is False
        assert "outcome_unknown" not in result

    def test_a_failed_submission_still_reports_a_job_id_key(self):
        """Callers read ``job_id`` unconditionally; a failure must not KeyError."""
        with patch(
            "spn_client.client.requests.get",
            side_effect=Exception("boom"),
        ):
            result = wayback.submit("https://example.com/")
        assert result["submitted"] is False
        assert result["job_id"] is None


class TestCheckJobStatus:
    """Reading the outcome of an SPN2 capture."""

    JOB = "spn2-abc123"

    def setup_method(self):
        wayback.reset_rate_limit_state()

    def teardown_method(self):
        wayback.reset_rate_limit_state()

    def _call(self, response=None, side_effect=None, **kwargs):
        opts = {"access_key": "AK", "secret_key": "SK"}
        opts.update(kwargs)
        with (
            patch(
                "spn_client.client.requests.get",
                return_value=response,
                side_effect=side_effect,
            ) as mock_get,
            patch.object(wayback, "_MIN_INTERVAL_SECONDS", 0.0),
            patch.object(wayback, "_BACKOFF_BASE_SECONDS", 0.0),
        ):
            return wayback.check_job_status(self.JOB, **opts), mock_get

    def test_success_builds_the_snapshot_url_from_the_answer(self):
        result, _ = self._call(
            _json_response(
                {
                    "status": "success",
                    "job_id": JOB_ID,
                    "timestamp": "20260905121627",
                    "original_url": "https://example.com/",
                }
            )
        )
        assert result["state"] == "success"
        assert (
            result["snapshot_url"]
            == "https://web.archive.org/web/20260905121627/https://example.com/"
        )
        assert result["snapshot_ts"] == "20260905121627"

    def test_success_without_a_timestamp_does_not_invent_a_url(self):
        """Half an answer is not an answer. A fabricated snapshot URL is worse
        than admitting we cannot name one — the author would click it."""
        result, _ = self._call(_json_response({"status": "success"}))
        assert result["state"] == "unknown"
        assert "snapshot_url" not in result

    def test_pending_is_pending(self):
        result, _ = self._call(_json_response({"status": "pending"}))
        assert result["state"] == "pending"

    def test_an_error_reports_archive_orgs_own_reason(self):
        result, _ = self._call(
            _json_response(
                {
                    "status": "error",
                    "status_ext": "error:invalid-url-syntax",
                    "message": "Cannot resolve host example.invalid.",
                }
            )
        )
        assert result["state"] == "failed"
        assert result["reason"] == "Cannot resolve host example.invalid."

    def test_an_error_falls_back_through_the_optional_fields(self):
        """SPN2 spreads the explanation over message/status_ext/exception and
        does not always send the friendliest one."""
        result, _ = self._call(
            _json_response({"status": "error", "status_ext": "error:no-access"})
        )
        assert result["state"] == "failed"
        assert result["reason"] == "error:no-access"

    def test_an_unrecognized_status_is_unknown_not_failed(self):
        result, _ = self._call(_json_response({"status": "something-new"}))
        assert result["state"] == "unknown"
        assert "something-new" in result["reason"]

    def test_a_non_json_answer_says_what_arrived(self):
        """Same misdirection guard as the availability API: reporting the JSON
        decoder's complaint sends the reader hunting for a parser bug."""
        resp = _json_response({}, content=b"x" * 512)
        resp.json.side_effect = ValueError("Expecting value: line 1 column 1")
        result, _ = self._call(resp)
        assert result["state"] == "unknown"
        assert "non-JSON" in result["reason"]
        assert "512 bytes" in result["reason"]
        assert "Expecting value" not in result["reason"]

    def test_a_401_is_reported_as_unknown_not_as_a_failed_capture(self):
        """Verified against the live endpoint 2026-09-05: it answers 401 to an
        unauthenticated caller and to a wrong credential alike. Rendering that
        as "the capture failed" would blame archive.org for our own config."""
        result, _ = self._call(_error_response(401))
        assert result["state"] == "unknown"
        assert "401" in result["reason"]
        assert "unknown, not failed" in result["reason"]

    def test_a_transport_error_never_raises(self):
        result, _ = self._call(side_effect=Exception("connection reset"))
        assert result["state"] == "unknown"
        assert result["reason"]

    def test_the_reason_is_a_sentence_and_the_raw_error_is_kept_beside_it(self):
        """A real run put this in the report verbatim:

            HTTPSConnectionPool(host='web.archive.org', port=443): Max retries
            exceeded with url: /save/status/... (Caused by NewConnectionError(
            ... [WinError 10061] ...))

        That is a debugger's string in a document written for someone deciding
        what to publish. The sentence goes in the report; the raw text stays
        available for whoever is actually debugging.
        """
        result, _ = self._call(
            side_effect=requests.exceptions.ConnectionError(
                "HTTPSConnectionPool(host='web.archive.org', port=443): Max "
                "retries exceeded (Caused by NewConnectionError(...WinError 10061...))"
            )
        )
        assert result["reason"] == (
            "could not reach archive.org about this capture — the connection "
            "was refused, dropped, or the host did not resolve"
        )
        assert "HTTPSConnectionPool" not in result["reason"]
        assert "HTTPSConnectionPool" in result["raw_error"]

    def test_a_timeout_says_so_in_words(self):
        result, _ = self._call(
            side_effect=requests.exceptions.ReadTimeout("Read timed out.")
        )
        assert "did not answer" in result["reason"]

    def test_the_secret_is_redacted_out_of_an_error(self):
        result, _ = self._call(
            side_effect=Exception("auth failed for SK456"), secret_key="SK456"
        )
        assert "SK456" not in result["reason"]

    def test_no_credentials_means_no_call_at_all(self):
        """The endpoint is credential-only (probed 2026-09-05), so asking
        without them spends pacing budget to be told 401."""
        result, mock_get = self._call(
            _json_response({"status": "pending"}), access_key=None, secret_key=None
        )
        assert result["state"] == "not_checked"
        assert "requires credentials" in result["reason"]
        mock_get.assert_not_called()

    def test_no_job_id_means_no_call_at_all(self):
        with patch(
            "spn_client.client.requests.get"
        ) as mock_get:
            result = wayback.check_job_status(None, access_key="AK", secret_key="SK")
        assert result["state"] == "not_checked"
        mock_get.assert_not_called()

    def test_a_tripped_breaker_skips_the_lookup(self):
        wayback._rate_limited_lookups = wayback._CIRCUIT_TRIP_AFTER
        result, mock_get = self._call(_json_response({"status": "pending"}))
        assert result["state"] == "not_checked"
        assert "rate limit tripped" in result["reason"]
        mock_get.assert_not_called()

    def test_the_authorization_header_is_sent(self):
        _, mock_get = self._call(_json_response({"status": "pending"}))
        headers = mock_get.call_args.kwargs["headers"]
        assert headers["Authorization"] == "LOW AK:SK"
        assert headers["Accept"] == "application/json"


class TestCaptureOptions:
    """What we deliberately ask Save Page Now to do, and deliberately do not."""

    def _post(self, **kwargs):
        with patch(
            "spn_client.client.requests.post",
            return_value=_mock_response({"job_id": "spn2-x"}),
        ) as mock_post:
            wayback.submit(
                "https://example.com/", access_key="AK", secret_key="SK", **kwargs
            )
        return mock_post.call_args.kwargs["data"]

    def test_error_pages_are_not_archived(self):
        """`capture_all` defaults ON in archive.org's own form, and ON means
        "archive it even if it answers 4xx/5xx". For a citation that manufactures
        a false archive: a source that 403s the capture would get its block page
        saved, and the next run would report the citation as archived."""
        assert self._post()["capture_all"] == "0"

    def test_archive_org_is_not_asked_to_re_decide_what_to_capture(self):
        """``if_not_archived_within`` looks like a fit for
        ``wayback_snapshot_stale_days`` and is not. Live 2026-09-06 it made
        archive.org answer ``job_id: null`` with "The same snapshot had been made
        177 hours ago" while the identical request without it captured — so it
        only ever removes information, and it is a second gate on a decision this
        pass has already made."""
        assert "if_not_archived_within" not in self._post(stale_days=180)

    def test_an_accepted_request_that_starts_no_capture_says_why(self):
        """archive.org answers 200 with a null job_id and an explanation."""
        with patch(
            "spn_client.client.requests.post",
            return_value=_mock_response(
                {
                    "url": "https://example.com/",
                    "job_id": None,
                    "message": "The same snapshot had been made 177 hours ago.",
                }
            ),
        ):
            result = wayback.submit(
                "https://example.com/", access_key="AK", secret_key="SK"
            )
        assert result["submitted"] is True
        assert result["job_id"] is None
        assert "177 hours ago" in result["error_summary"]

    def test_nothing_emails_the_operator_or_touches_their_archive(self):
        """A run submits many citations. `email_result` and `wacz` would each
        send mail per capture, and `wm-save-mywebarchive` writes to the
        operator's own account — none of those are side effects a review gets to
        cause on its own."""
        data = self._post()
        for never in ("email_result", "wacz", "wm-save-mywebarchive"):
            assert never not in data

    def test_no_outlink_or_screenshot_load_is_added(self):
        data = self._post()
        assert "capture_outlinks" not in data
        assert "capture_screenshot" not in data

    def test_the_url_is_still_sent(self):
        assert self._post()["url"] == "https://example.com/"


class TestSubmitHonoursTheBreaker:
    def setup_method(self):
        wayback.reset_rate_limit_state()

    def teardown_method(self):
        wayback.reset_rate_limit_state()

    def test_a_tripped_breaker_stops_submissions_too(self):
        """Submissions used to ignore the breaker entirely: once five lookups
        had been refused the availability API went quiet while Save Page Now —
        the more expensive call, since it starts a real capture — kept firing."""
        wayback._rate_limited_lookups = wayback._CIRCUIT_TRIP_AFTER
        with (
            patch(
                "spn_client.client.requests.get"
            ) as mock_get,
            patch(
                "spn_client.client.requests.post"
            ) as mock_post,
        ):
            result = wayback.submit("https://example.com/")
        mock_get.assert_not_called()
        mock_post.assert_not_called()
        assert result["submitted"] is False
        assert result["rate_limited"] is True
        assert result["archived"] is False

    def test_an_untripped_breaker_submits_normally(self):
        with patch(
            "spn_client.client.requests.get",
            return_value=_mock_response({}),
        ) as mock_get:
            result = wayback.submit("https://example.com/")
        mock_get.assert_called_once()
        assert result["submitted"] is True


class TestCaptureCapacity:
    """archive.org will say how much capacity the account has. Every concurrency
    number governing archiving was otherwise invented."""

    def setup_method(self):
        wayback.reset_rate_limit_state()

    def teardown_method(self):
        wayback.reset_rate_limit_state()

    LIVE = {
        "processing": 0,
        "available": 3,
        "daily_captures": 49,
        "daily_captures_limit": 30000,
    }

    def _call(self, payload=None, side_effect=None, **kw):
        opts = {"access_key": "AK", "secret_key": "SK"}
        opts.update(kw)
        with (
            patch(
                "spn_client.client.requests.get",
                return_value=_json_response(payload) if payload else None,
                side_effect=side_effect,
            ) as mock_get,
            patch.object(wayback, "_MIN_INTERVAL_SECONDS", 0.0),
        ):
            return wayback.capture_capacity(**opts), mock_get

    def test_the_real_payload_shape_parses(self):
        """Measured live 2026-09-06 — this is verbatim what archive.org sent."""
        cap, _ = self._call(self.LIVE)
        assert cap["known"] is True
        assert cap["available"] == 3
        assert cap["daily_captures"] == 49
        assert cap["daily_captures_limit"] == 30000
        assert cap["daily_exhausted"] is False

    def test_an_exhausted_quota_is_recognised(self):
        cap, _ = self._call({**self.LIVE, "daily_captures": 30000})
        assert cap["daily_exhausted"] is True

    def test_unknown_never_collapses_into_exhausted(self):
        """ "We could not find out" must not stop the run archiving."""
        cap, _ = self._call({})
        assert cap["daily_exhausted"] is False
        assert cap["available"] is None

    def test_no_credentials_means_no_call(self):
        cap, mock_get = self._call(self.LIVE, access_key=None, secret_key=None)
        assert cap["known"] is False
        assert "authenticated" in cap["reason"]
        mock_get.assert_not_called()

    def test_a_tripped_breaker_skips_the_lookup(self):
        wayback._rate_limited_lookups = wayback._CIRCUIT_TRIP_AFTER
        cap, mock_get = self._call(self.LIVE)
        assert cap["known"] is False
        mock_get.assert_not_called()

    def test_a_transport_failure_is_a_sentence_not_a_stack(self):
        cap, _ = self._call(side_effect=requests.exceptions.ConnectionError("boom"))
        assert cap["known"] is False
        assert "could not reach archive.org" in cap["reason"]


class TestJobStatusSharesTheOneRateLimitScheme:
    """archive.org throttles per IP, not per endpoint.

    The job-status endpoint is a second archive.org caller added to a module
    that already had pacing, backoff and a circuit breaker. Giving it its own
    would mean two schemes each pacing against half the real request rate —
    which is how you get rate-limited while believing you are being polite.
    These tests pin it to the existing one.
    """

    def setup_method(self):
        wayback.reset_rate_limit_state()

    def teardown_method(self):
        wayback.reset_rate_limit_state()

    def test_refusals_come_out_of_the_one_shared_breaker_budget(self):
        """Three refused availability lookups plus two refused status lookups
        trip the breaker at five. Two separate budgets would not trip at all."""
        with (
            patch(
                "spn_client.client.requests.get",
                return_value=_rate_limited_response(),
            ),
            patch.object(wayback, "_MIN_INTERVAL_SECONDS", 0.0),
            patch.object(wayback, "_BACKOFF_BASE_SECONDS", 0.0),
        ):
            for i in range(3):
                wayback.check(f"https://example.com/{i}")
            assert wayback.rate_limited_out() is False
            for i in range(2):
                wayback.check_job_status(f"spn2-{i}", access_key="AK", secret_key="SK")

        assert wayback._rate_limited_lookups == wayback._CIRCUIT_TRIP_AFTER
        assert wayback.rate_limited_out() is True

    def test_a_429_from_a_status_call_backs_off_every_other_caller(self):
        """The backoff clock is shared, so a throttled status call slows the
        availability lookups too — not just itself."""
        with (
            patch(
                "spn_client.client.requests.get",
                return_value=_rate_limited_response(retry_after="30"),
            ),
            patch.object(wayback, "_MIN_INTERVAL_SECONDS", 0.0),
            patch.object(wayback, "_BACKOFF_BASE_SECONDS", 0.0),
            patch.object(wayback, "_MAX_ATTEMPTS", 1),
        ):
            before = time.monotonic()
            wayback.check_job_status(JOB_ID, access_key="AK", secret_key="SK")

        assert wayback._blocked_until >= before + 30.0


class TestSnapshotRawUrl:
    """``id_`` serves the original captured bytes; the ordinary form serves them
    wrapped in archive.org's banner and its wombat.js URL rewriter."""

    def test_the_id_form_is_produced(self):
        got = wayback.snapshot_raw_url(
            "http://web.archive.org/web/20260905123736/https://example.org/a"
        )
        assert got == (
            "https://web.archive.org/web/20260905123736id_/https://example.org/a"
        )

    def test_a_non_snapshot_url_yields_nothing(self):
        assert wayback.snapshot_raw_url("https://example.org/a") is None
        assert wayback.snapshot_raw_url("") is None
        assert wayback.snapshot_raw_url(None) is None

    def test_flagged_snapshot_forms_are_normalised(self):
        """Wayback URLs can already carry a mode flag (im_, js_, cs_)."""
        got = wayback.snapshot_raw_url(
            "https://web.archive.org/web/20200101000000im_/https://example.org/a"
        )
        assert "20200101000000id_/" in got


class TestSnapshotStatusIsKept:
    """The availability API reports the captured response's own HTTP status, and
    this discarded it. A snapshot of a 403 block page renders exactly like a
    snapshot of the document."""

    def _check(self, closest):
        payload = {"archived_snapshots": {"closest": closest}}
        with patch(
            "spn_client.client.requests.get",
            return_value=_mock_response(payload),
        ):
            return wayback.check("https://example.org/a")

    def _closest(self, **over):
        c = {
            "available": True,
            "url": "https://web.archive.org/web/20260905000000/https://example.org/a",
            "timestamp": "20260905000000",
        }
        c.update(over)
        return c

    def test_a_good_capture_is_marked_as_such(self):
        r = self._check(self._closest(status="200"))
        assert r["snapshot_status"] == "200"
        assert r["snapshot_is_error_capture"] is False

    def test_an_archived_error_page_is_flagged(self):
        r = self._check(self._closest(status="403"))
        assert r["snapshot_status"] == "403"
        assert r["snapshot_is_error_capture"] is True

    def test_an_absent_status_asserts_nothing(self):
        r = self._check(self._closest())
        assert "snapshot_status" not in r
        assert "snapshot_is_error_capture" not in r


class TestSystemStatus:
    """A 520, a read timeout and a refused connection look identical from inside
    the pipeline, and each is equally consistent with "we asked too often" and
    "the service is unwell". This is what tells them apart."""

    def setup_method(self):
        wayback.reset_rate_limit_state()

    def teardown_method(self):
        wayback.reset_rate_limit_state()

    LIVE = {
        "recent_captures": 941,
        "status": "ok",
        "queues": {"spn2-captures": 0, "spn2-api": 0, "spn2-screenshots": 0},
    }

    def _call(self, payload=None, side_effect=None):
        with (
            patch(
                "spn_client.client.requests.get",
                return_value=_json_response(payload) if payload is not None else None,
                side_effect=side_effect,
            ) as mock_get,
            patch.object(wayback, "_MIN_INTERVAL_SECONDS", 0.0),
        ):
            return wayback.system_status(), mock_get

    def test_the_real_payload_shape_parses(self):
        """Measured live 2026-09-06 — verbatim what archive.org sent."""
        st, _ = self._call(self.LIVE)
        assert st["known"] is True
        assert st["ok"] is True
        assert st["recent_captures"] == 941
        assert st["busiest_queue"] is None

    def test_the_deepest_backlog_is_identified(self):
        st, _ = self._call(
            {**self.LIVE, "queues": {"spn2-captures": 12, "spn2-api": 300}}
        )
        assert st["busiest_queue"] == ("spn2-api", 300)

    def test_a_degraded_service_is_reported_as_such(self):
        st, _ = self._call({**self.LIVE, "status": "degraded"})
        assert st["ok"] is False
        assert st["status"] == "degraded"

    def test_a_tripped_breaker_asks_nothing(self):
        """Diagnosis is not exempt from the budget it is diagnosing."""
        wayback._rate_limited_lookups = wayback._CIRCUIT_TRIP_AFTER
        st, mock_get = self._call(self.LIVE)
        assert st["known"] is False
        mock_get.assert_not_called()

    def test_a_transport_failure_is_a_sentence(self):
        st, _ = self._call(side_effect=requests.exceptions.ConnectionError("x"))
        assert st["known"] is False
        assert "could not reach archive.org" in st["reason"]


class TestServiceHealthNote:
    def test_an_unwell_service_takes_the_blame(self):
        note = wayback.service_health_note(
            {"known": True, "ok": False, "status": "degraded"}
        )
        assert "degraded" in note
        assert "not the caller" in note

    def test_a_healthy_service_says_so(self):
        note = wayback.service_health_note(
            {"known": True, "ok": True, "status": "ok", "busiest_queue": None}
        )
        assert "healthy" in note

    def test_healthy_but_backed_up_is_worth_saying(self):
        note = wayback.service_health_note(
            {
                "known": True,
                "ok": True,
                "status": "ok",
                "busiest_queue": ("spn2-api", 900),
            }
        )
        assert "busy" in note
        assert "900" in note

    def test_an_unknown_verdict_adds_nothing(self):
        """Appending "we could not tell" to a failure the reader is already
        looking at is noise, not information."""
        assert wayback.service_health_note({"known": False}) is None
        assert wayback.service_health_note(None) is None
