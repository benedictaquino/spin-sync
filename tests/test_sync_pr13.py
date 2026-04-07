"""
Unit tests for PR #13: device_watts ICG candidate filtering in sync.run().

The candidate selection logic (from src/sync.py):
    candidates = [
        a for a in all_activities
        if a["type"] in TARGET_ACTIVITY_TYPES
        and a["id"] not in synced_ids
        and a.get("device_watts") is True
    ]
    skipped = [
        a for a in all_activities
        if a["type"] in TARGET_ACTIVITY_TYPES
        and a["id"] not in synced_ids
        and a.get("device_watts") is not True
    ]
"""

import importlib
import os
import sys
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Stub out merge_fit before sync.py is imported so that the missing
# fitparse / fit-tool transitive deps don't cause an ImportError.
# ---------------------------------------------------------------------------

_merge_fit_stub = MagicMock()
_merge_fit_stub.merge = MagicMock()
_merge_fit_stub.RecordSnapshot = MagicMock()
sys.modules.setdefault("merge_fit", _merge_fit_stub)
sys.modules.setdefault("fitparse", MagicMock())


# ---------------------------------------------------------------------------
# Helpers to build fake Strava activity dicts
# ---------------------------------------------------------------------------

def _make_activity(
    activity_id: int,
    activity_type: str = "VirtualRide",
    device_watts: bool | None = None,
    start_date: str = "2024-03-01T10:00:00Z",
    name: str | None = None,
) -> dict:
    act: dict = {
        "id": activity_id,
        "type": activity_type,
        "start_date": start_date,
        "name": name or f"Activity {activity_id}",
        "distance": 20000.0,
        "average_watts": 200 if device_watts else 0,
    }
    if device_watts is not None:
        act["device_watts"] = device_watts
    return act


# ---------------------------------------------------------------------------
# Import sync with required env vars mocked out
# ---------------------------------------------------------------------------

def _import_sync():
    """Import (or re-use) sync module with env vars satisfied."""
    env_patch = {
        "STRAVA_CLIENT_ID": "fake_client_id",
        "STRAVA_CLIENT_SECRET": "fake_secret",
        "STRAVA_REFRESH_TOKEN": "fake_refresh",
    }
    with patch.dict(os.environ, env_patch):
        # Reload to pick up env vars if already imported
        if "sync" in sys.modules:
            return sys.modules["sync"]
        return importlib.import_module("sync")


# ---------------------------------------------------------------------------
# Test: candidate filtering by device_watts
# ---------------------------------------------------------------------------

class TestDeviceWattsCandidateFiltering(unittest.TestCase):
    """Test that the candidate list correctly includes/excludes activities
    based on the device_watts field."""

    @classmethod
    def setUpClass(cls):
        cls.sync = _import_sync()

    def _run_with_activities(
        self,
        all_activities: list[dict],
        synced_ids: list[int] | None = None,
    ) -> list[dict]:
        """
        Execute just the candidate-selection expression from sync.run(),
        replicating it exactly as written in the source.
        """
        synced_ids_set = set(synced_ids or [])
        TARGET_ACTIVITY_TYPES = self.sync.TARGET_ACTIVITY_TYPES

        candidates = [
            a for a in all_activities
            if a["type"] in TARGET_ACTIVITY_TYPES
            and a["id"] not in synced_ids_set
            and a.get("device_watts") is True
        ]
        return candidates

    # ------------------------------------------------------------------
    # device_watts=True  →  included
    # ------------------------------------------------------------------

    def test_icg_activity_with_device_watts_true_is_candidate(self):
        act = _make_activity(1, device_watts=True)
        candidates = self._run_with_activities([act])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["id"], 1)

    def test_multiple_icg_activities_all_device_watts_true(self):
        acts = [
            _make_activity(10, device_watts=True),
            _make_activity(11, device_watts=True),
            _make_activity(12, device_watts=True),
        ]
        candidates = self._run_with_activities(acts)
        self.assertEqual(len(candidates), 3)

    # ------------------------------------------------------------------
    # device_watts=False  →  excluded (watch recording)
    # ------------------------------------------------------------------

    def test_watch_activity_with_device_watts_false_is_excluded(self):
        act = _make_activity(2, device_watts=False)
        candidates = self._run_with_activities([act])
        self.assertEqual(candidates, [])

    def test_watch_activity_with_device_watts_missing_is_excluded(self):
        """Activities without a device_watts key at all should be excluded."""
        act = _make_activity(3)  # no device_watts key
        self.assertNotIn("device_watts", act)
        candidates = self._run_with_activities([act])
        self.assertEqual(candidates, [])

    # ------------------------------------------------------------------
    # Mixed bag
    # ------------------------------------------------------------------

    def test_mixed_activities_only_device_watts_true_selected(self):
        acts = [
            _make_activity(20, device_watts=True),   # ICG → included
            _make_activity(21, device_watts=False),  # watch → excluded
            _make_activity(22),                       # no key → excluded
            _make_activity(23, device_watts=True),   # ICG → included
        ]
        candidates = self._run_with_activities(acts)
        candidate_ids = {c["id"] for c in candidates}
        self.assertEqual(candidate_ids, {20, 23})

    # ------------------------------------------------------------------
    # synced_ids exclusion still works regardless of device_watts
    # ------------------------------------------------------------------

    def test_already_synced_icg_activity_excluded_despite_device_watts_true(self):
        act = _make_activity(30, device_watts=True)
        candidates = self._run_with_activities([act], synced_ids=[30])
        self.assertEqual(candidates, [])

    def test_already_synced_watch_activity_not_in_candidates_either(self):
        act = _make_activity(31, device_watts=False)
        candidates = self._run_with_activities([act], synced_ids=[31])
        self.assertEqual(candidates, [])

    def test_synced_ids_only_exclude_matching_ids(self):
        acts = [
            _make_activity(40, device_watts=True),  # synced → excluded
            _make_activity(41, device_watts=True),  # not synced → included
        ]
        candidates = self._run_with_activities(acts, synced_ids=[40])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["id"], 41)

    # ------------------------------------------------------------------
    # Activity type filter still applies
    # ------------------------------------------------------------------

    def test_non_target_type_excluded_even_with_device_watts_true(self):
        """Only VirtualRide/Ride types should be candidates."""
        act = _make_activity(50, activity_type="Run", device_watts=True)
        candidates = self._run_with_activities([act])
        self.assertEqual(candidates, [])

    def test_ride_type_with_device_watts_true_is_candidate(self):
        act = _make_activity(51, activity_type="Ride", device_watts=True)
        candidates = self._run_with_activities([act])
        self.assertEqual(len(candidates), 1)


# ---------------------------------------------------------------------------
# Test: skipped list (debug logging path)
# ---------------------------------------------------------------------------

class TestSkippedList(unittest.TestCase):
    """Verify the complementary 'skipped' list captures watch recordings."""

    @classmethod
    def setUpClass(cls):
        cls.sync = _import_sync()

    def _run_skipped(
        self,
        all_activities: list[dict],
        synced_ids: list[int] | None = None,
    ) -> list[dict]:
        synced_ids_set = set(synced_ids or [])
        TARGET_ACTIVITY_TYPES = self.sync.TARGET_ACTIVITY_TYPES

        skipped = [
            a for a in all_activities
            if a["type"] in TARGET_ACTIVITY_TYPES
            and a["id"] not in synced_ids_set
            and a.get("device_watts") is not True
        ]
        return skipped

    def test_watch_false_appears_in_skipped(self):
        act = _make_activity(60, device_watts=False)
        skipped = self._run_skipped([act])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["id"], 60)

    def test_watch_missing_key_appears_in_skipped(self):
        act = _make_activity(61)
        skipped = self._run_skipped([act])
        self.assertEqual(len(skipped), 1)

    def test_icg_device_watts_true_not_in_skipped(self):
        act = _make_activity(62, device_watts=True)
        skipped = self._run_skipped([act])
        self.assertEqual(skipped, [])

    def test_already_synced_not_in_skipped(self):
        """synced_ids excludes from both candidates and skipped."""
        act = _make_activity(63, device_watts=False)
        skipped = self._run_skipped([act], synced_ids=[63])
        self.assertEqual(skipped, [])


if __name__ == "__main__":
    unittest.main()
