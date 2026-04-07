"""Tests for ICG candidate filtering logic in sync.py.

Covers the device_watts filter added in PR #13:
- Only activities with device_watts=True are selected as ICG candidates
- Watch recordings (device_watts absent or False) are skipped with debug logging
- Previously-synced activities are excluded regardless of device_watts
"""

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Set required env vars BEFORE importing sync (it reads them at module level)
os.environ.setdefault("STRAVA_CLIENT_ID", "fake_id")
os.environ.setdefault("STRAVA_CLIENT_SECRET", "fake_secret")
os.environ.setdefault("STRAVA_REFRESH_TOKEN", "fake_token")
os.environ.setdefault("GARMIN_SESSION_FILE", "/tmp/fake-garmin-session.json")

# Add src/ to path so we can import sync
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import sync  # noqa: E402


def _make_activity(
    activity_id: int,
    activity_type: str = "VirtualRide",
    device_watts: bool | None = None,
    name: str = "Test Activity",
    start_date: str = "2025-01-15T10:00:00Z",
    average_watts: float = 0,
    distance: float = 10000,
) -> dict:
    """Helper to build a fake Strava activity dict."""
    act = {
        "id": activity_id,
        "type": activity_type,
        "name": name,
        "start_date": start_date,
        "average_watts": average_watts,
        "distance": distance,
    }
    if device_watts is not None:
        act["device_watts"] = device_watts
    return act


class TestCandidateFilteringWatchOnly:
    """Test plan item 1: only watch recordings present -- no candidates selected."""

    @patch("sync.save_state")
    @patch("sync.load_state")
    @patch("sync.strava_get_recent_activities")
    @patch("sync.strava_refresh_access_token")
    def test_watch_recordings_not_selected_as_candidates(
        self, mock_token, mock_activities, mock_load, mock_save, caplog
    ):
        """When only watch recordings (no device_watts) are present,
        no candidates should be selected and debug logs show skipped activities."""
        mock_token.return_value = "fake_access_token"
        mock_activities.return_value = [
            _make_activity(1001, device_watts=False, name="Watch Ride 1"),
            _make_activity(1002, device_watts=None, name="Watch Ride 2"),
        ]
        mock_load.return_value = {"synced_ids": [], "last_run_epoch": 0}

        with caplog.at_level(logging.DEBUG, logger="spin-sync"):
            sync.run()

        # Should have logged "0 new ICG candidate(s)"
        assert any("0 new ICG candidate(s)" in r.message for r in caplog.records), (
            "Expected log message about 0 candidates"
        )

        # Should have debug-logged skipped activities
        skipped_logs = [
            r for r in caplog.records
            if "device_watts not set" in r.message
        ]
        assert len(skipped_logs) == 2, (
            f"Expected 2 skipped debug logs, got {len(skipped_logs)}"
        )
        assert "Watch Ride 1" in skipped_logs[0].message
        assert "Watch Ride 2" in skipped_logs[1].message

        # State should still be saved (with updated last_run_epoch)
        mock_save.assert_called_once()

    @patch("sync.save_state")
    @patch("sync.load_state")
    @patch("sync.strava_get_recent_activities")
    @patch("sync.strava_refresh_access_token")
    def test_device_watts_false_excluded(
        self, mock_token, mock_activities, mock_load, mock_save
    ):
        """Activities with device_watts=False should not be candidates."""
        mock_token.return_value = "fake_access_token"
        mock_activities.return_value = [
            _make_activity(2001, device_watts=False),
        ]
        mock_load.return_value = {"synced_ids": [], "last_run_epoch": 0}

        sync.run()

        # No processing should have occurred -- save_state called with
        # no new synced_ids
        saved_state = mock_save.call_args[0][0]
        assert 2001 not in saved_state["synced_ids"]


class TestCandidateFilteringICG:
    """Test plan item 2: real ICG activity (device_watts=True) is picked up."""

    @patch("sync.garmin_upload")
    @patch("sync.garmin_delete_activity")
    @patch("sync.merge")
    @patch("sync.garmin_download_fit")
    @patch("sync.garmin_find_matching_activity")
    @patch("sync.strava_delete_activity")
    @patch("sync.strava_find_watch_duplicate")
    @patch("sync.strava_fetch_icg_streams")
    @patch("sync.save_state")
    @patch("sync.load_state")
    @patch("sync.strava_get_recent_activities")
    @patch("sync.strava_refresh_access_token")
    def test_icg_activity_selected_as_candidate(
        self,
        mock_token,
        mock_activities,
        mock_load,
        mock_save,
        mock_streams,
        mock_find_dup,
        mock_delete_strava,
        mock_garmin_find,
        mock_garmin_dl,
        mock_merge,
        mock_garmin_delete,
        mock_garmin_upload,
        caplog,
    ):
        """An activity with device_watts=True should be processed as a candidate."""
        mock_token.return_value = "fake_access_token"
        icg_activity = _make_activity(
            3001, device_watts=True, name="ICG Spin Class"
        )
        mock_activities.return_value = [icg_activity]
        mock_load.return_value = {"synced_ids": [], "last_run_epoch": 0}

        # Mock the downstream pipeline to succeed
        mock_streams.return_value = [
            sync.RecordSnapshot(
                timestamp_ms=1705312800000, power=150, cadence=80, distance=100
            )
        ]
        mock_find_dup.return_value = None  # No Strava watch duplicate
        mock_garmin_find.return_value = {
            "activityId": 9999,
            "activityName": "Indoor Cycling",
        }
        mock_garmin_dl.return_value = True
        mock_garmin_upload.return_value = {}

        with caplog.at_level(logging.INFO, logger="spin-sync"):
            sync.run()

        # ICG streams should have been fetched for this activity
        mock_streams.assert_called_once()
        assert mock_streams.call_args[0][0] == 3001

        # Activity should be marked as synced
        saved_state = mock_save.call_args[0][0]
        assert 3001 in saved_state["synced_ids"]

        # Should have logged "1 new ICG candidate(s)"
        assert any("1 new ICG candidate(s)" in r.message for r in caplog.records)

    @patch("sync.save_state")
    @patch("sync.load_state")
    @patch("sync.strava_get_recent_activities")
    @patch("sync.strava_refresh_access_token")
    def test_mixed_activities_only_icg_selected(
        self, mock_token, mock_activities, mock_load, mock_save, caplog
    ):
        """With a mix of ICG and watch activities, only ICG (device_watts=True)
        should be candidates. Watch recordings should appear in skipped logs."""
        mock_token.return_value = "fake_access_token"
        mock_activities.return_value = [
            _make_activity(4001, device_watts=True, name="ICG Power Ride"),
            _make_activity(4002, device_watts=False, name="Watch Recording"),
            _make_activity(4003, device_watts=None, name="Another Watch"),
            _make_activity(4004, activity_type="Run", device_watts=None, name="Morning Run"),
        ]
        mock_load.return_value = {"synced_ids": [], "last_run_epoch": 0}

        # The ICG candidate will try to process -- mock streams to return None
        # so it skips gracefully
        with patch("sync.strava_fetch_icg_streams", return_value=None):
            with caplog.at_level(logging.DEBUG, logger="spin-sync"):
                sync.run()

        # Should have 1 candidate
        assert any("1 new ICG candidate(s)" in r.message for r in caplog.records)

        # Should have 2 skipped (4002 and 4003 -- 4004 is a Run, not in TARGET_ACTIVITY_TYPES)
        skipped_logs = [
            r for r in caplog.records if "device_watts not set" in r.message
        ]
        assert len(skipped_logs) == 2


class TestCandidateFilteringSyncedIds:
    """Test plan item 3: previously-synced activities excluded regardless of device_watts."""

    @patch("sync.save_state")
    @patch("sync.load_state")
    @patch("sync.strava_get_recent_activities")
    @patch("sync.strava_refresh_access_token")
    def test_synced_activity_excluded_even_with_device_watts(
        self, mock_token, mock_activities, mock_load, mock_save, caplog
    ):
        """An activity already in synced_ids should not be a candidate,
        even if device_watts=True."""
        mock_token.return_value = "fake_access_token"
        mock_activities.return_value = [
            _make_activity(5001, device_watts=True, name="Already Synced Ride"),
        ]
        mock_load.return_value = {"synced_ids": [5001], "last_run_epoch": 1000}

        with caplog.at_level(logging.DEBUG, logger="spin-sync"):
            sync.run()

        # Should have 0 candidates
        assert any("0 new ICG candidate(s)" in r.message for r in caplog.records)

        # Should NOT appear in skipped logs either (it's filtered by synced_ids first)
        skipped_logs = [
            r for r in caplog.records if "device_watts not set" in r.message
        ]
        assert len(skipped_logs) == 0

    @patch("sync.save_state")
    @patch("sync.load_state")
    @patch("sync.strava_get_recent_activities")
    @patch("sync.strava_refresh_access_token")
    def test_synced_watch_recording_excluded(
        self, mock_token, mock_activities, mock_load, mock_save, caplog
    ):
        """A watch recording that was previously synced should not appear
        in either candidates or skipped."""
        mock_token.return_value = "fake_access_token"
        mock_activities.return_value = [
            _make_activity(6001, device_watts=False, name="Old Watch Ride"),
        ]
        mock_load.return_value = {"synced_ids": [6001], "last_run_epoch": 1000}

        with caplog.at_level(logging.DEBUG, logger="spin-sync"):
            sync.run()

        assert any("0 new ICG candidate(s)" in r.message for r in caplog.records)
        skipped_logs = [
            r for r in caplog.records if "device_watts not set" in r.message
        ]
        assert len(skipped_logs) == 0
