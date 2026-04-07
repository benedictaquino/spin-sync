"""
Unit tests for PR #15: actionable error message for Strava 401 on activity delete.

Tests cover:
- 401 response -> returns False AND logs ERROR containing "strava_auth.py" and
  "STRAVA_REFRESH_TOKEN"
- 204 response -> returns True, no error logged
- 404 response -> returns True (already gone), no error logged
- run() logs a warning when strava_delete_activity returns False
"""

import importlib
import logging
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers to import sync with required env vars mocked out
# ---------------------------------------------------------------------------

REQUIRED_ENV = {
    "STRAVA_CLIENT_ID": "test_client_id",
    "STRAVA_CLIENT_SECRET": "test_client_secret",
    "STRAVA_REFRESH_TOKEN": "test_refresh_token",
}


def import_sync():
    """Import (or re-import) the sync module with required env vars set."""
    # Remove cached module so env vars are re-read at import time.
    for key in list(sys.modules.keys()):
        if key == "sync" or key.startswith("sync."):
            del sys.modules[key]

    with patch.dict("os.environ", REQUIRED_ENV, clear=False):
        # merge_fit is imported by sync; provide a minimal stub so we don't
        # need actual FIT infrastructure for these unit tests.
        merge_fit_stub = types.ModuleType("merge_fit")
        merge_fit_stub.merge = MagicMock()
        merge_fit_stub.RecordSnapshot = MagicMock()
        sys.modules["merge_fit"] = merge_fit_stub

        import sync  # noqa: PLC0415
        return sync


# ---------------------------------------------------------------------------
# strava_delete_activity tests
# ---------------------------------------------------------------------------

class TestStravaDeleteActivity:

    @pytest.fixture(autouse=True)
    def _sync(self):
        self.sync = import_sync()

    def _make_response(self, status_code: int, text: str = "") -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        return resp

    def test_204_returns_true_no_error(self, caplog):
        """204 No Content -> success, no error or warning logged."""
        resp = self._make_response(204)
        with patch.object(self.sync.requests, "delete", return_value=resp), \
             caplog.at_level(logging.ERROR, logger="spin-sync"):
            result = self.sync.strava_delete_activity(12345, "token")

        assert result is True
        # No ERROR-level messages should have been emitted.
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records == []

    def test_404_returns_true_no_error(self, caplog):
        """404 Not Found -> already gone, treated as success, no error logged."""
        resp = self._make_response(404)
        with patch.object(self.sync.requests, "delete", return_value=resp), \
             caplog.at_level(logging.ERROR, logger="spin-sync"):
            result = self.sync.strava_delete_activity(99999, "token")

        assert result is True
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records == []

    def test_401_returns_false(self, caplog):
        """401 Unauthorized -> returns False."""
        resp = self._make_response(401)
        with patch.object(self.sync.requests, "delete", return_value=resp), \
             caplog.at_level(logging.DEBUG, logger="spin-sync"):
            result = self.sync.strava_delete_activity(11111, "bad_token")

        assert result is False

    def test_401_logs_error_with_strava_auth_script(self, caplog):
        """401 -> ERROR log mentions scripts/strava_auth.py."""
        resp = self._make_response(401)
        with patch.object(self.sync.requests, "delete", return_value=resp), \
             caplog.at_level(logging.ERROR, logger="spin-sync"):
            self.sync.strava_delete_activity(11111, "bad_token")

        error_messages = " ".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR
        )
        assert "strava_auth.py" in error_messages, (
            f"Expected 'strava_auth.py' in error log. Got: {error_messages!r}"
        )

    def test_401_logs_error_with_strava_refresh_token(self, caplog):
        """401 -> ERROR log mentions STRAVA_REFRESH_TOKEN."""
        resp = self._make_response(401)
        with patch.object(self.sync.requests, "delete", return_value=resp), \
             caplog.at_level(logging.ERROR, logger="spin-sync"):
            self.sync.strava_delete_activity(11111, "bad_token")

        error_messages = " ".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR
        )
        assert "STRAVA_REFRESH_TOKEN" in error_messages, (
            f"Expected 'STRAVA_REFRESH_TOKEN' in error log. Got: {error_messages!r}"
        )

    def test_other_error_returns_false_no_error_log(self, caplog):
        """Non-401/404/204 -> returns False, logs WARNING (not ERROR)."""
        resp = self._make_response(500, "Internal Server Error")
        with patch.object(self.sync.requests, "delete", return_value=resp), \
             caplog.at_level(logging.DEBUG, logger="spin-sync"):
            result = self.sync.strava_delete_activity(77777, "token")

        assert result is False
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records == [], "500 should log WARNING, not ERROR"


# ---------------------------------------------------------------------------
# run() warning when strava_delete_activity returns False
# ---------------------------------------------------------------------------

class TestRunWarnsOnDeleteFailure:

    @pytest.fixture(autouse=True)
    def _sync(self):
        self.sync = import_sync()

    def test_run_logs_warning_when_delete_fails(self, caplog, tmp_path):
        """run() should log a WARNING when strava_delete_activity returns False."""

        state_file = tmp_path / "state.json"
        garmin_session_file = tmp_path / "garmin_session.json"
        # Write a minimal Garmin session file so GarminSession doesn't raise.
        garmin_session_file.write_text('{"cookies": [], "user_agent": "test"}')

        icg_activity = {
            "id": 42,
            "type": "VirtualRide",
            "name": "Morning Spin",
            "start_date": "2024-01-15T08:00:00Z",
            "distance": 20000,
            "average_watts": 200,
            "device_watts": True,
        }
        watch_duplicate = {
            "id": 99,
            "type": "VirtualRide",
            "name": "Watch Ride",
            "start_date": "2024-01-15T08:01:00Z",
            "average_watts": 0,
        }

        fake_records = [MagicMock()]

        with patch.dict("os.environ", {"STATE_FILE": str(state_file),
                                        "GARMIN_SESSION_FILE": str(garmin_session_file)}), \
             patch.object(self.sync, "strava_refresh_access_token", return_value="tok"), \
             patch.object(self.sync, "strava_get_recent_activities",
                          return_value=[icg_activity, watch_duplicate]), \
             patch.object(self.sync, "strava_fetch_icg_streams", return_value=fake_records), \
             patch.object(self.sync, "strava_find_watch_duplicate", return_value=watch_duplicate), \
             patch.object(self.sync, "strava_delete_activity", return_value=False), \
             patch.object(self.sync, "garmin_find_matching_activity", return_value=None), \
             caplog.at_level(logging.WARNING, logger="spin-sync"):
            self.sync.run()

        warning_messages = " ".join(
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        )
        assert "NOT deleted" in warning_messages or "not deleted" in warning_messages.lower(), (
            f"Expected deletion-failure warning in run(). Got: {warning_messages!r}"
        )
