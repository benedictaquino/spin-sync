"""
spin-sync: Auto-sync ICG IC7 spin workouts from Strava to Garmin Connect.

Flow:
  1. Poll Strava for recent VirtualRide / indoor cycling activities from ICG.
  2. Fetch ICG power/cadence/distance streams from the Strava API.
  3. Find the matching watch activity in Garmin Connect and download its .fit file.
  4. Merge: inject ICG power + cadence into the Garmin watch file second-by-second.
  5. Delete the original empty watch activity from Garmin Connect.
  6. Upload the merged file to Garmin Connect.
  7. Record the Strava activity ID in state to avoid re-processing.

Result:
  - Strava : original ICG recording kept as-is (Strava API does not support deletion,
             so the Garmin watch auto-sync remains there too)
  - Garmin : one activity (merged file — watch HR/Training Effect + ICG power/cadence)
"""

import base64
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
from playwright.sync_api import sync_playwright

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


# ---------------------------------------------------------------------------
# Garmin Connect helpers
# ---------------------------------------------------------------------------

class GarminSession:
    """
    Garmin Connect API client that drives a persistent Chromium browser via
    Playwright.  All API calls are made as browser-side fetch() calls so that
    Garmin's Cloudflare protection and CSRF requirements are satisfied
    automatically — no manual cookie or token wrangling needed.
    """

    BASE = "https://connect.garmin.com"
    BROWSER_PROFILE_DIR = Path.home() / ".spin-sync-chromium-profile"

    def __init__(self, session_file: Path) -> None:
        if not session_file.exists():
            raise FileNotFoundError(
                f"Garmin session file not found: {session_file}\n"
                "Run  scripts/garmin_auth.py  to authenticate via browser first."
            )
        session_data = json.loads(session_file.read_text())
        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            str(self.BROWSER_PROFILE_DIR),
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        if session_data.get("cookies"):
            self._context.add_cookies(session_data["cookies"])
        self._page = (
            self._context.pages[0] if self._context.pages else self._context.new_page()
        )
        self._csrf_token: str | None = None
        self._init_session()

    def _init_session(self) -> None:
        """Navigate to Garmin Connect to establish the browser session and capture CSRF token."""
        captured: dict = {}

        def on_request(request: object) -> None:
            if "gc-api/" in request.url and not captured.get("csrf"):  # type: ignore[attr-defined]
                captured["csrf"] = request.headers.get("connect-csrf-token")  # type: ignore[attr-defined]

        self._page.on("request", on_request)
        self._page.goto(f"{self.BASE}/app/activities", wait_until="networkidle", timeout=30_000)
        self._page.remove_listener("request", on_request)
        self._csrf_token = captured.get("csrf")
        log.info("Garmin browser session ready (CSRF: %s…)", (self._csrf_token or "none")[:8])

    def _fetch(self, method: str, url: str) -> str:
        """Run an HTTP request inside the browser via fetch(), returning the response body."""
        result = self._page.evaluate(
            """async ({method, url, csrf}) => {
                const resp = await fetch(url, {
                    method,
                    headers: {
                        'NK': 'NT',
                        'Accept': 'application/json',
                        'connect-csrf-token': csrf || '',
                    },
                });
                return {status: resp.status, body: await resp.text()};
            }""",
            {"method": method, "url": url, "csrf": self._csrf_token},
        )
        if result["status"] >= 400:
            raise requests.HTTPError(f"HTTP {result['status']} {method} {url}")
        return result["body"]

    def close(self) -> None:
        self._context.close()
        self._pw.stop()

    def get_activities_by_date(self, start_date: str, end_date: str) -> list[dict]:
        url = (
            f"{self.BASE}/gc-api/activitylist-service/activities/search/activities"
            f"?startDate={start_date}&endDate={end_date}&limit=100"
        )
        return json.loads(self._fetch("GET", url))

    def download_fit(self, activity_id: int) -> bytes:
        result = self._page.evaluate(
            """async ({url, csrf}) => {
                const resp = await fetch(url, {
                    headers: {'connect-csrf-token': csrf || ''},
                });
                if (!resp.ok) return {status: resp.status, data: null};
                const buf = await resp.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let bin = '';
                for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
                return {status: resp.status, data: btoa(bin)};
            }""",
            {
                "url": f"{self.BASE}/gc-api/download-service/files/activity/{activity_id}",
                "csrf": self._csrf_token,
            },
        )
        if result["status"] >= 400:
            raise requests.HTTPError(f"HTTP {result['status']} downloading activity {activity_id}")
        return base64.b64decode(result["data"])

    def delete_activity(self, activity_id: int) -> None:
        self._fetch("DELETE", f"{self.BASE}/gc-api/activity-service/activity/{activity_id}")

    def upload_fit(self, fit_path: Path) -> dict:
        fit_b64 = base64.b64encode(fit_path.read_bytes()).decode()
        result = self._page.evaluate(
            """async ({url, filename, data, csrf}) => {
                const bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0));
                const blob = new Blob([bytes], {type: 'application/octet-stream'});
                const form = new FormData();
                form.append('file', blob, filename);
                const resp = await fetch(url, {
                    method: 'POST',
                    headers: {'connect-csrf-token': csrf || ''},
                    body: form,
                });
                return {status: resp.status, body: await resp.text()};
            }""",
            {
                "url": f"{self.BASE}/gc-api/upload-service/upload",
                "filename": fit_path.name,
                "data": fit_b64,
                "csrf": self._csrf_token,
            },
        )
        if result["status"] >= 400:
            raise requests.HTTPError(f"HTTP {result['status']} uploading {fit_path.name}")
        return json.loads(result["body"]) if result["body"] else {}


_garmin_session: GarminSession | None = None


def garmin_session() -> GarminSession:
    global _garmin_session
    if _garmin_session is None:
        _garmin_session = GarminSession(GARMIN_SESSION_FILE)
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

    unmatched_types = {
        (act.get("activityType", {}).get("typeKey") or "").lower()
        for act in activities
        if (act.get("activityType", {}).get("typeKey") or "").lower()
        not in GARMIN_INDOOR_ACTIVITY_TYPES
    }
    log.warning(
        "No matching Garmin indoor activity found within %ds of ICG start. "
        "Unrecognized types seen: %s — expected one of: %s",
        TIME_MATCH_TOLERANCE_S,
        sorted(unmatched_types) if unmatched_types else "(none)",
        sorted(GARMIN_INDOOR_ACTIVITY_TYPES),
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

            # 2. Find matching Garmin Connect watch activity
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

            # 3. Download Garmin watch .fit
            if not garmin_download_fit(garmin_act_id, garmin_fit):
                log.error("Could not download Garmin watch file %s.", garmin_act_id)
                continue

            # 4. Merge ICG power/cadence into Garmin watch file
            try:
                merge(garmin_fit, icg_records, merged_fit)
            except Exception as exc:
                log.error("Merge failed for '%s': %s", activity_name, exc)
                continue

            # 5. Delete the original empty watch activity from Garmin Connect
            garmin_delete_activity(garmin_act_id)

            # 6. Upload merged file to Garmin Connect
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
    if _garmin_session is not None:
        _garmin_session.close()
    log.info("Done.")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        log.exception("Unhandled error: %s", exc)
        sys.exit(1)
