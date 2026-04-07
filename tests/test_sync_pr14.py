"""
Unit tests for PR #14: expand Garmin activity types and add diagnostic logging.

Covers:
- fitness_equipment and other type activities now match
- indoor_cycling, cardio, cycling still match (regression)
- Unknown types don't match
- Debug log shows all returned activities with id, name, type, start_time
- Warning on no-match shows returned types AND expected set
"""

import importlib
import logging
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Module-level setup: set required env vars before importing sync.py
# ---------------------------------------------------------------------------

REQUIRED_ENV = {
    "STRAVA_CLIENT_ID": "test_client_id",
    "STRAVA_CLIENT_SECRET": "test_client_secret",
    "STRAVA_REFRESH_TOKEN": "test_refresh_token",
    "GARMIN_SESSION_FILE": "/tmp/fake-garmin-session.json",
    "STATE_FILE": "/tmp/fake-spin-sync-state.json",
}

for key, val in REQUIRED_ENV.items():
    os.environ.setdefault(key, val)


def _load_sync():
    """Import (or reimport) sync with mocked GarminSession and merge_fit."""
    # Stub out merge_fit so sync.py doesn't need the real package
    merge_fit_stub = types.ModuleType("merge_fit")
    merge_fit_stub.merge = MagicMock()
    merge_fit_stub.RecordSnapshot = MagicMock
    sys.modules["merge_fit"] = merge_fit_stub

    if "sync" in sys.modules:
        del sys.modules["sync"]

    import sync as _sync
    return _sync


sync = _load_sync()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_EPOCH = 1_700_000_000  # arbitrary fixed timestamp


def _make_garmin_activity(
    activity_id: int,
    name: str,
    type_key: str,
    start_gmt: str,
) -> dict:
    return {
        "activityId": activity_id,
        "activityName": name,
        "activityType": {"typeKey": type_key},
        "startTimeGMT": start_gmt,
    }


def _patch_garmin_session(activities: list[dict]):
    """Return a context-manager patch that makes garmin_session() return a
    mock whose get_activities_by_date returns *activities*."""
    mock_session = MagicMock()
    mock_session.get_activities_by_date.return_value = activities
    return patch.object(sync, "garmin_session", return_value=mock_session)


# ---------------------------------------------------------------------------
# Tests: activity type matching
# ---------------------------------------------------------------------------

class TestGarminActivityTypeMatching:
    """garmin_find_matching_activity should accept the types in
    GARMIN_INDOOR_ACTIVITY_TYPES and reject anything else."""

    # Start time exactly at _BASE_EPOCH → gap = 0 → within tolerance
    _START_GMT = "2023-11-14 22:13:20"  # == datetime.utcfromtimestamp(_BASE_EPOCH)

    def _run(self, type_key: str) -> dict | None:
        activity = _make_garmin_activity(
            activity_id=1,
            name="Morning Ride",
            type_key=type_key,
            start_gmt=self._START_GMT,
        )
        with _patch_garmin_session([activity]):
            return sync.garmin_find_matching_activity(_BASE_EPOCH)

    # --- newly added types (the core PR change) ---

    def test_fitness_equipment_matches(self):
        assert self._run("fitness_equipment") is not None

    def test_other_matches(self):
        assert self._run("other") is not None

    # --- pre-existing types (regression) ---

    def test_indoor_cycling_matches(self):
        assert self._run("indoor_cycling") is not None

    def test_cardio_matches(self):
        assert self._run("cardio") is not None

    def test_cycling_matches(self):
        assert self._run("cycling") is not None

    # --- unknown types should NOT match ---

    def test_running_does_not_match(self):
        assert self._run("running") is None

    def test_swimming_does_not_match(self):
        assert self._run("swimming") is None

    def test_unknown_type_does_not_match(self):
        assert self._run("unknown_type_xyz") is None

    def test_empty_type_does_not_match(self):
        assert self._run("") is None

    # --- type matching is case-insensitive via .lower() ---

    def test_fitness_equipment_uppercase_matches(self):
        assert self._run("FITNESS_EQUIPMENT") is not None

    def test_indoor_cycling_mixed_case_matches(self):
        assert self._run("Indoor_Cycling") is not None


# ---------------------------------------------------------------------------
# Tests: GARMIN_INDOOR_ACTIVITY_TYPES constant
# ---------------------------------------------------------------------------

class TestGarminIndoorActivityTypesConstant:
    def test_fitness_equipment_in_constant(self):
        assert "fitness_equipment" in sync.GARMIN_INDOOR_ACTIVITY_TYPES

    def test_other_in_constant(self):
        assert "other" in sync.GARMIN_INDOOR_ACTIVITY_TYPES

    def test_indoor_cycling_in_constant(self):
        assert "indoor_cycling" in sync.GARMIN_INDOOR_ACTIVITY_TYPES

    def test_cardio_in_constant(self):
        assert "cardio" in sync.GARMIN_INDOOR_ACTIVITY_TYPES

    def test_cycling_in_constant(self):
        assert "cycling" in sync.GARMIN_INDOOR_ACTIVITY_TYPES


# ---------------------------------------------------------------------------
# Tests: debug logging — returned activities list
# ---------------------------------------------------------------------------

class TestDebugLogging:
    """garmin_find_matching_activity should emit a DEBUG log that contains
    each returned activity's id, name, type, and start_time."""

    _START_GMT = "2023-11-14 22:13:20"

    def test_debug_log_contains_activity_id(self, caplog):
        activities = [
            _make_garmin_activity(99, "Spin Class", "indoor_cycling", self._START_GMT)
        ]
        with _patch_garmin_session(activities):
            with caplog.at_level(logging.DEBUG, logger="spin-sync"):
                sync.garmin_find_matching_activity(_BASE_EPOCH)

        assert "99" in caplog.text

    def test_debug_log_contains_activity_name(self, caplog):
        activities = [
            _make_garmin_activity(99, "Morning Spin", "indoor_cycling", self._START_GMT)
        ]
        with _patch_garmin_session(activities):
            with caplog.at_level(logging.DEBUG, logger="spin-sync"):
                sync.garmin_find_matching_activity(_BASE_EPOCH)

        assert "Morning Spin" in caplog.text

    def test_debug_log_contains_activity_type(self, caplog):
        activities = [
            _make_garmin_activity(99, "Spin", "fitness_equipment", self._START_GMT)
        ]
        with _patch_garmin_session(activities):
            with caplog.at_level(logging.DEBUG, logger="spin-sync"):
                sync.garmin_find_matching_activity(_BASE_EPOCH)

        assert "fitness_equipment" in caplog.text

    def test_debug_log_contains_start_time(self, caplog):
        activities = [
            _make_garmin_activity(99, "Spin", "indoor_cycling", self._START_GMT)
        ]
        with _patch_garmin_session(activities):
            with caplog.at_level(logging.DEBUG, logger="spin-sync"):
                sync.garmin_find_matching_activity(_BASE_EPOCH)

        assert self._START_GMT in caplog.text

    def test_debug_log_shows_count(self, caplog):
        activities = [
            _make_garmin_activity(1, "A", "indoor_cycling", self._START_GMT),
            _make_garmin_activity(2, "B", "running", self._START_GMT),
        ]
        with _patch_garmin_session(activities):
            with caplog.at_level(logging.DEBUG, logger="spin-sync"):
                sync.garmin_find_matching_activity(_BASE_EPOCH)

        # "2 activities" or "2 activit..." — the count should appear
        assert "2" in caplog.text

    def test_debug_log_emitted_at_debug_level(self, caplog):
        """Debug log should NOT appear at INFO level."""
        activities = [
            _make_garmin_activity(99, "Spin", "indoor_cycling", self._START_GMT)
        ]
        with _patch_garmin_session(activities):
            with caplog.at_level(logging.INFO, logger="spin-sync"):
                sync.garmin_find_matching_activity(_BASE_EPOCH)

        # At INFO level, debug messages should not be captured
        assert "activityId" not in caplog.text


# ---------------------------------------------------------------------------
# Tests: no-match warning shows returned types AND expected set
# ---------------------------------------------------------------------------

class TestNoMatchWarning:
    """When no activity matches, the WARNING log must include:
    1. The actual types returned by Garmin
    2. The expected GARMIN_INDOOR_ACTIVITY_TYPES set
    """

    _START_GMT = "2023-11-14 22:13:20"

    def _run_no_match(self, caplog, type_key: str = "running"):
        """Run with an activity that won't match and capture WARNING logs."""
        activities = [
            _make_garmin_activity(1, "Morning Run", type_key, self._START_GMT)
        ]
        with _patch_garmin_session(activities):
            with caplog.at_level(logging.WARNING, logger="spin-sync"):
                result = sync.garmin_find_matching_activity(_BASE_EPOCH)
        return result

    def test_no_match_returns_none(self, caplog):
        assert self._run_no_match(caplog) is None

    def test_no_match_warning_contains_returned_type(self, caplog):
        self._run_no_match(caplog, type_key="running")
        assert "running" in caplog.text

    def test_no_match_warning_contains_expected_types(self, caplog):
        self._run_no_match(caplog)
        # At least one of the expected types should appear in the warning
        assert any(
            t in caplog.text
            for t in sync.GARMIN_INDOOR_ACTIVITY_TYPES
        )

    def test_no_match_warning_mentions_fitness_equipment_in_expected(self, caplog):
        """fitness_equipment must appear in the 'expected' part of the warning."""
        self._run_no_match(caplog)
        assert "fitness_equipment" in caplog.text

    def test_no_match_warning_mentions_other_in_expected(self, caplog):
        """'other' must appear in the 'expected' part of the warning."""
        self._run_no_match(caplog)
        assert "other" in caplog.text

    def test_no_match_warning_with_empty_activities(self, caplog):
        """Empty activity list → warning mentions (none) for returned types."""
        with _patch_garmin_session([]):
            with caplog.at_level(logging.WARNING, logger="spin-sync"):
                result = sync.garmin_find_matching_activity(_BASE_EPOCH)

        assert result is None
        assert "none" in caplog.text.lower()

    def test_no_match_warning_with_multiple_non_matching_types(self, caplog):
        """Multiple non-matching types all appear in the warning."""
        activities = [
            _make_garmin_activity(1, "Run", "running", self._START_GMT),
            _make_garmin_activity(2, "Swim", "swimming", self._START_GMT),
        ]
        with _patch_garmin_session(activities):
            with caplog.at_level(logging.WARNING, logger="spin-sync"):
                sync.garmin_find_matching_activity(_BASE_EPOCH)

        assert "running" in caplog.text
        assert "swimming" in caplog.text

    def test_no_match_warning_mentions_tolerance(self, caplog):
        """Warning should mention the tolerance window."""
        self._run_no_match(caplog)
        # The warning mentions TIME_MATCH_TOLERANCE_S value
        assert str(sync.TIME_MATCH_TOLERANCE_S) in caplog.text
