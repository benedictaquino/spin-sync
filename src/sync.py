"""
spin-sync: Auto-sync ICG IC7 spin workouts from Strava to Garmin Connect.

Flow:
  1. Poll Strava for recent VirtualRide / indoor cycling activities from ICG.
  2. Fetch ICG power/cadence/distance streams from the Strava API.
  2. Fetch ICG power/cadence/distance streams from the Strava API.
  3. Find the matching Indoor Cycling activity the Garmin watch auto-synced to
     Strava (same day, overlapping time window) and delete it — it's empty
     (no power/cadence) and we don't want the duplicate.
  4. Find the same watch activity in Garmin Connect and download its .fit file.
  5. Merge: inject ICG power + cadence into the Garmin watch file second-by-second.
  6. Delete the original empty watch activity from Garmin Connect.
  7. Upload the merged file to Garmin Connect.
  8. Record the Strava activity ID in state to avoid re-processing.

Result:
  - Strava : one activity (the original ICG recording with all power/cadence data)
  - Garmin : one activity (merged file — watch HR/Training Effect + ICG power/cadence)
"""

import os
import sys
import json
import logging
import tempfile
import time
import zipfile
import io
from datetime import datetime, timezone
from pathlib import Path

import requests

from merge_fit import merge, RecordSnapshot
from merge_fit import merge, RecordSnapshot

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
log = logging.getLogger("spin-sync")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STRAVA_CLIENT_ID     = os.environ["STRAVA_CLIENT_ID"]
STRAVA_CLIENT_SECRET = os.environ["STRAVA_CLIENT_SECRET"]
STRAVA_REFRESH_TOKEN = os.environ["STRAVA_REFRESH_TOKEN"]

GARMIN_SESSION_FILE = Path(
    os.environ.get("GARMIN_SESSION_FILE", Path.home() / ".spin-sync-garmin-session.json")
)

# Strava types produced by the ICG app
TARGET_ACTIVITY_TYPES = {"VirtualRide", "Ride"}

# Strava / Garmin activity types that indicate a plain watch recording
# (the empty duplicate we want to remove)
WATCH_ACTIVITY_TYPES_STRAVA = {"Ride", "VirtualRide", "workout"}
# Includes "fitness_equipment" and "other" to cover the Garmin watch's reported
# activity type when recording a spin class without a specific sport profile.
GARMIN_INDOOR_ACTIVITY_TYPES = {"indoor_cycling", "cardio", "cycling", "fitness_equipment", "other"}

# How far back to look on the very first run (seconds)
LOOKBACK_SECONDS = int(os.environ.get("LOOKBACK_SECONDS", str(6 * 3600)))

# Maximum gap between ICG activity start and watch activity start
# to still be considered the same workout
TIME_MATCH_TOLERANCE_S = int(os.environ.get("TIME_MATCH_TOLERANCE_S", "600"))  # 10 min

STATE_FILE = Path(os.environ.get("STATE_FILE", Path.home() / ".spin-sync-state.json"))


# ---------------------------------------------------------------------------
# Strava helpers
# ---------------------------------------------------------------------------

def strava_refresh_access_token() -> str:
    resp = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id":     STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "grant_type":    "refresh_token",
            "refresh_token": STRAVA_REFRESH_TOKEN,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    new_refresh = data.get("refresh_token", STRAVA_REFRESH_TOKEN)
    if new_refresh != STRAVA_REFRESH_TOKEN:
        log.info("Strava refresh token rotated — update STRAVA_REFRESH_TOKEN.")
        Path(".strava_refresh_token").write_text(new_refresh)
    return data["access_token"]


def strava_get_recent_activities(access_token: str, after_epoch: int) -> list[dict]:
    resp = requests.get(
        "https://www.strava.com/api/v3/athlete/activities",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"after": after_epoch, "per_page": 30},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def strava_activity_start_epoch(activity: dict) -> int:
    dt = datetime.fromisoformat(activity["start_date"].replace("Z", "+00:00"))
    return int(dt.timestamp())


def strava_fetch_icg_streams(
    activity_id: int, access_token: str, start_epoch: int,
    total_distance_m: float = 0,
) -> list[RecordSnapshot] | None:
    """Fetch power/cadence/distance/time streams from Strava API.
    Returns list of RecordSnapshot, or None on failure.

    total_distance_m: total activity distance in metres (from the activity
    object).  Used as a fallback when Strava does not return a distance stream
    — cumulative distance is then linearly interpolated from elapsed time.
    """
    resp = requests.get(
        f"https://www.strava.com/api/v3/activities/{activity_id}/streams",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"keys": "watts,cadence,time,distance", "key_by_type": "true"},
        timeout=30,
    )
    if resp.status_code == 404:
        log.warning("No streams for Strava activity %s.", activity_id)
        return None
    resp.raise_for_status()
    data = resp.json()

    time_data = data.get("time", {}).get("data", [])
    watts     = data.get("watts", {}).get("data", [])
    cadence   = data.get("cadence", {}).get("data", [])
    distance  = data.get("distance", {}).get("data", [])

    if not time_data:
        log.warning("Strava streams for %s have no time data.", activity_id)
        return None

    # Fall back to linear interpolation when the distance stream is absent.
    if not distance and total_distance_m > 0:
        total_time = time_data[-1] or 1
        distance = [total_distance_m * t / total_time for t in time_data]
        log.info(
            "No distance stream for %s; interpolated from total distance %.0f m.",
            activity_id, total_distance_m,
        )

    start_ms = start_epoch * 1000
    records = []
    for i, t in enumerate(time_data):
        records.append(RecordSnapshot(
            timestamp_ms=start_ms + int(t * 1000),
            power=watts[i]    if i < len(watts)    else None,
            cadence=cadence[i] if i < len(cadence) else None,
            distance=distance[i] if i < len(distance) else None,
        ))

    log.info("Fetched %d stream samples for Strava activity %s.", len(records), activity_id)
    return records
    log.info("Fetched %d stream samples for Strava activity %s.", len(records), activity_id)
    return records


def strava_find_watch_duplicate(
    access_token: str,
    icg_activity: dict,
    all_activities: list[dict],
) -> dict | None:
    """
    Find the Garmin watch activity on Strava that is a duplicate of the ICG
    session — i.e. a ride/workout starting within TIME_MATCH_TOLERANCE_S of
    the ICG activity but NOT the ICG activity itself.

    The watch activity will have no power data (average_watts is 0 or absent)
    which is the distinguishing characteristic vs the ICG recording.
    """
    icg_start  = strava_activity_start_epoch(icg_activity)
    icg_id     = icg_activity["id"]

    for act in all_activities:
        if act["id"] == icg_id:
            continue
        if act.get("type") not in WATCH_ACTIVITY_TYPES_STRAVA:
            continue

        gap_s = abs(strava_activity_start_epoch(act) - icg_start)
        if gap_s > TIME_MATCH_TOLERANCE_S:
            continue

        # Guard: don't delete an activity that actually has power data
        if act.get("average_watts", 0) > 0:
            log.info(
                "Skipping Strava activity %s as duplicate candidate — it has "
                "power data (avg %.0fW). Leaving it intact.",
                act["id"], act["average_watts"],
            )
            continue

        log.info(
            "Found Strava watch duplicate: '%s' (id=%s, gap=%.0fs, avg_watts=%s)",
            act.get("name"), act["id"], gap_s, act.get("average_watts", 0),
        )
        return act

    return None


def strava_delete_activity(activity_id: int, access_token: str) -> bool:
    """
    Delete a Strava activity. Requires the activity:write scope.
    Returns True on success or if already gone.
    """
    resp = requests.delete(
        f"https://www.strava.com/api/v3/activities/{activity_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if resp.status_code == 204:
        log.info("Deleted Strava activity %s.", activity_id)
        return True
    if resp.status_code == 404:
        log.info("Strava activity %s already gone.", activity_id)
        return True
    log.warning(
        "Could not delete Strava activity %s: HTTP %s %s",
        activity_id, resp.status_code, resp.text[:200],
    )
    return False


# ---------------------------------------------------------------------------
# Garmin Connect helpers
# ---------------------------------------------------------------------------

class GarminSession:
    """
    Thin Garmin Connect API client that authenticates via browser session
    cookies saved by scripts/garmin_auth.py.

    Uses the same Connect web endpoints as the garminconnect library, but
    loads credentials from a cookie file rather than performing an SSO login
    (which Cloudflare now blocks for automated clients).
    """

    BASE = "https://connect.garmin.com"

    def __init__(self, session_file: Path) -> None:
        if not session_file.exists():
            raise FileNotFoundError(
                f"Garmin session file not found: {session_file}\n"
                "Run  scripts/garmin_auth.py  to authenticate via browser first."
            )
        self._session = requests.Session()
        self._session.headers.update({
            "NK": "NT",
            "X-app-ver": "4.82.0.0",
            "Accept": "application/json",
        })
        self._load_cookies(session_file)

    def _load_cookies(self, session_file: Path) -> None:
        data = json.loads(session_file.read_text())
        # Set cookies as a header to bypass requests' domain-matching logic,
        # which silently drops cookies whose domain doesn't exactly match the
        # jar's expectations (e.g. JWT_WEB on .connect.garmin.com).
        cookie_header = "; ".join(
            f"{c['name']}={c['value']}" for c in data["cookies"]
        )
        self._session.headers["Cookie"] = cookie_header
        # cf_clearance is bound to the IP + User Agent that solved the
        # Cloudflare challenge. Use the exact UA from the Playwright session.
        if ua := data.get("user_agent"):
            self._session.headers["User-Agent"] = ua

    def get_activities_by_date(self, start_date: str, end_date: str) -> list[dict]:
        resp = self._session.get(
            f"{self.BASE}/proxy/activitylist-service/activities/search/activities",
            params={"startDate": start_date, "endDate": end_date, "limit": 100},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def download_fit(self, activity_id: int) -> bytes:
        resp = self._session.get(
            f"{self.BASE}/download-service/files/activity/{activity_id}",
            timeout=30,
        )
        resp.raise_for_status()
        return resp.content

    def delete_activity(self, activity_id: int) -> None:
        resp = self._session.delete(
            f"{self.BASE}/activity-service/activity/{activity_id}",
            timeout=15,
        )
        resp.raise_for_status()

    def upload_fit(self, fit_path: Path) -> dict:
        with open(fit_path, "rb") as f:
            resp = self._session.post(
                f"{self.BASE}/upload-service/upload",
                files={"file": (fit_path.name, f, "application/octet-stream")},
                timeout=60,
            )
        resp.raise_for_status()
        return resp.json()


_garmin_session: GarminSession | None = None


def garmin_session() -> GarminSession:
    global _garmin_session
    if _garmin_session is None:
        _garmin_session = GarminSession(GARMIN_SESSION_FILE)
        log.info("Loaded Garmin session from %s", GARMIN_SESSION_FILE)
    return _garmin_session


def garmin_find_matching_activity(start_epoch: int) -> dict | None:
    """
    Find the indoor cycling / cardio watch activity in Garmin Connect that
    corresponds to the ICG session.
    """
    date_str = datetime.fromtimestamp(
        start_epoch - TIME_MATCH_TOLERANCE_S, tz=timezone.utc
    ).strftime("%Y-%m-%d")

    try:
        activities = garmin_session().get_activities_by_date(date_str, date_str)
    except Exception as exc:
        log.warning("Could not fetch Garmin activities for %s: %s", date_str, exc)
        return None

    log.debug(
        "Garmin returned %d activit%s for %s: %s",
        len(activities),
        "y" if len(activities) == 1 else "ies",
        date_str,
        [
            {
                "activityId": a.get("activityId"),
                "activityName": a.get("activityName"),
                "type": (a.get("activityType", {}).get("typeKey") or ""),
                "startTimeGMT": a.get("startTimeGMT"),
            }
            for a in activities
        ],
    )

    for act in activities:
        act_type = (act.get("activityType", {}).get("typeKey") or "").lower()
        if act_type not in GARMIN_INDOOR_ACTIVITY_TYPES:
            continue

        act_start_raw = act.get("startTimeGMT") or act.get("startTimeLocal") or ""
        try:
            act_start_epoch = int(
                datetime.fromisoformat(
                    act_start_raw.replace(" ", "T") + "+00:00"
                ).timestamp()
            )
        except ValueError:
            continue

        gap_s = abs(act_start_epoch - start_epoch)
        if gap_s <= TIME_MATCH_TOLERANCE_S:
            log.info(
                "Matched Garmin activity '%s' (id=%s, type=%s, gap=%.0fs)",
                act.get("activityName"), act.get("activityId"), act_type, gap_s,
            )
            return act

    log.warning(
        "No matching Garmin indoor activity found within %ds of ICG start.",
        TIME_MATCH_TOLERANCE_S,
    )
    return None


def garmin_download_fit(activity_id: int, dest: Path) -> bool:
    try:
        data = garmin_session().download_fit(activity_id)
    except Exception as exc:
        log.error("Failed to download Garmin activity %s: %s", activity_id, exc)
        return False

    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            fit_names = [n for n in zf.namelist() if n.endswith(".fit")]
            if not fit_names:
                return False
            dest.write_bytes(zf.read(fit_names[0]))
    else:
        dest.write_bytes(data)

    log.info("Downloaded Garmin .fit (%d bytes) → %s", dest.stat().st_size, dest.name)
    return True


def garmin_delete_activity(activity_id: int) -> None:
    try:
        garmin_session().delete_activity(activity_id)
        log.info("Deleted Garmin activity %s.", activity_id)
    except Exception as exc:
        log.warning("Could not delete Garmin activity %s: %s", activity_id, exc)


def garmin_upload(fit_path: Path, activity_name: str) -> None:
    result = garmin_session().upload_fit(fit_path)
    log.info("Uploaded '%s' to Garmin Connect: %s", activity_name, result)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"synced_ids": [], "last_run_epoch": 0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    state       = load_state()
    synced_ids  = set(state.get("synced_ids", []))
    last_run    = state.get("last_run_epoch", 0)
    after_epoch = last_run if last_run else int(time.time()) - LOOKBACK_SECONDS
    now_epoch   = int(time.time())

    log.info(
        "Checking Strava for new activities since %s …",
        datetime.fromtimestamp(after_epoch, tz=timezone.utc).isoformat(),
    )

    access_token   = strava_refresh_access_token()
    all_activities = strava_get_recent_activities(access_token, after_epoch)
    candidates     = [
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
    for a in skipped:
        log.debug(
            "Skipping '%s' (Strava id=%s): device_watts not set, likely a watch recording.",
            a.get("name", a["id"]), a["id"],
        )
    log.info("Found %d total activities, %d new ICG candidate(s).", len(all_activities), len(candidates))

    for icg_activity in candidates:
        strava_id     = icg_activity["id"]
        activity_name = icg_activity.get("name", f"Spin {strava_id}")
        start_epoch   = strava_activity_start_epoch(icg_activity)

        log.info(
            "Processing '%s' (Strava id=%s, start=%s)",
            activity_name, strava_id,
            datetime.fromtimestamp(start_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp        = Path(tmpdir)
            garmin_fit = tmp / "garmin_watch.fit"
            merged_fit = tmp / "merged.fit"

            # 1. Fetch ICG power/cadence/distance streams from Strava
            icg_records = strava_fetch_icg_streams(
                strava_id, access_token, start_epoch,
                total_distance_m=icg_activity.get("distance", 0),
            )
            if icg_records is None:
                log.warning("Skipping %s — no streams on Strava.", strava_id)
                synced_ids.add(strava_id)
                continue

            # 2. Find and delete the empty Garmin watch duplicate on Strava
            strava_duplicate = strava_find_watch_duplicate(
                access_token, icg_activity, all_activities
            )
            if strava_duplicate:
                strava_delete_activity(strava_duplicate["id"], access_token)
            else:
                log.info(
                    "No Strava watch duplicate found for '%s' — either it hasn't "
                    "synced yet or was already deleted. Continuing.",
                    activity_name,
                )

            # 3. Find matching Garmin Connect watch activity
            garmin_act = garmin_find_matching_activity(start_epoch)
            if garmin_act is None:
                log.warning(
                    "No Garmin watch activity found for '%s'. "
                    "Skipping merge — nothing to do on Garmin side.",
                    activity_name,
                )
                synced_ids.add(strava_id)
                continue

            garmin_act_id = garmin_act["activityId"]

            # 4. Download Garmin watch .fit
            if not garmin_download_fit(garmin_act_id, garmin_fit):
                log.error("Could not download Garmin watch file %s.", garmin_act_id)
                continue

            # 5. Merge ICG power/cadence into Garmin watch file
            try:
                merge(garmin_fit, icg_records, merged_fit)
            except Exception as exc:
                log.error("Merge failed for '%s': %s", activity_name, exc)
                continue

            # 6. Delete the original empty watch activity from Garmin Connect
            garmin_delete_activity(garmin_act_id)

            # 7. Upload merged file to Garmin Connect
            try:
                garmin_upload(merged_fit, activity_name)
                synced_ids.add(strava_id)
                log.info(
                    "✓  '%s' done — Strava has ICG original, "
                    "Garmin Connect has merged file with Training Effect + power.",
                    activity_name,
                )
            except Exception as exc:
                log.error("Garmin upload failed for '%s': %s", activity_name, exc)

    state["synced_ids"]     = sorted(synced_ids)
    state["last_run_epoch"] = now_epoch
    save_state(state)
    log.info("Done.")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        log.exception("Unhandled error: %s", exc)
        sys.exit(1)
