"""
merge_local.py — Download a Strava ICG activity's power streams + a Garmin
watch FIT file, merge them, and write the result locally for manual upload.

Usage:
    python scripts/merge_local.py <strava_activity_id> <garmin_activity_id> [output.fit]
    python scripts/merge_local.py <strava_activity_id> --garmin-fit <path/to/watch.fit> [output.fit]

  strava_activity_id  Strava activity ID for the ICG spin bike recording
  garmin_activity_id  Garmin Connect activity ID for the empty watch recording
                      (skip with --garmin-fit if Garmin API is rate-limiting you)
  --garmin-fit FILE   Use a pre-downloaded Garmin watch .fit instead of the API
  output.fit          Output path (default: merged_<strava_id>.fit)

To download the Garmin .fit manually:
  Garmin Connect → Activity → ... menu → Export Original

Environment variables (same as sync.py / .env):
    STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN
    GARMIN_EMAIL, GARMIN_PASSWORD  (not needed with --garmin-fit)
"""

import io
import logging
import os
import sys
import zipfile
from pathlib import Path

import requests
from garminconnect import Garmin

# Allow importing from src/
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from merge_fit import RecordSnapshot, merge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("merge-local")

# ---------------------------------------------------------------------------
# Strava
# ---------------------------------------------------------------------------

def strava_access_token() -> str:
    resp = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id":     os.environ["STRAVA_CLIENT_ID"],
            "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
            "grant_type":    "refresh_token",
            "refresh_token": os.environ["STRAVA_REFRESH_TOKEN"],
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def strava_fetch_streams(activity_id: int, token: str) -> list[RecordSnapshot]:
    resp = requests.get(
        f"https://www.strava.com/api/v3/activities/{activity_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    start_epoch = int(
        __import__("datetime").datetime
        .fromisoformat(resp.json()["start_date"].replace("Z", "+00:00"))
        .timestamp()
    )

    resp = requests.get(
        f"https://www.strava.com/api/v3/activities/{activity_id}/streams",
        headers={"Authorization": f"Bearer {token}"},
        params={"keys": "watts,cadence,time,distance", "key_by_type": "true"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    time_data = data.get("time", {}).get("data", [])
    watts     = data.get("watts", {}).get("data", [])
    cadence   = data.get("cadence", {}).get("data", [])
    distance  = data.get("distance", {}).get("data", [])

    if not time_data:
        raise RuntimeError(f"No time stream data for Strava activity {activity_id}")

    start_ms = start_epoch * 1000
    records = [
        RecordSnapshot(
            timestamp_ms=start_ms + int(t * 1000),
            power=watts[i]    if i < len(watts)    else None,
            cadence=cadence[i] if i < len(cadence) else None,
            distance=distance[i] if i < len(distance) else None,
        )
        for i, t in enumerate(time_data)
    ]
    log.info("Fetched %d stream samples from Strava activity %s.", len(records), activity_id)
    return records


# ---------------------------------------------------------------------------
# Garmin
# ---------------------------------------------------------------------------

GARMIN_TOKENSTORE = Path.home() / ".garth"


def garmin_login() -> Garmin:
    """Login to Garmin Connect, reusing cached tokens when available."""
    client = Garmin(
        os.environ["GARMIN_EMAIL"],
        os.environ["GARMIN_PASSWORD"],
        is_cn=False,
    )
    if GARMIN_TOKENSTORE.exists():
        try:
            client.login(str(GARMIN_TOKENSTORE))
            log.info("Logged in to Garmin Connect (cached tokens).")
            return client
        except Exception as exc:
            log.info("Cached tokens invalid (%s), re-authenticating.", exc)

    client.login()
    client.garth.dump(str(GARMIN_TOKENSTORE))
    log.info("Logged in to Garmin Connect (fresh). Tokens saved to %s.", GARMIN_TOKENSTORE)
    return client


def garmin_download_fit(activity_id: int, dest: Path) -> None:
    client = garmin_login()

    data = client.download_activity(
        activity_id, dl_fmt=client.ActivityDownloadFormat.ORIGINAL
    )

    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            fit_names = [n for n in zf.namelist() if n.endswith(".fit")]
            if not fit_names:
                raise RuntimeError("No .fit file found inside Garmin zip download.")
            dest.write_bytes(zf.read(fit_names[0]))
    else:
        dest.write_bytes(data)

    log.info("Downloaded Garmin .fit (%d bytes) → %s", dest.stat().st_size, dest)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    args = sys.argv[1:]

    # Parse --garmin-fit <path>
    garmin_fit_override: Path | None = None
    if "--garmin-fit" in args:
        idx = args.index("--garmin-fit")
        if idx + 1 >= len(args):
            print("--garmin-fit requires a file path argument")
            sys.exit(1)
        garmin_fit_override = Path(args.pop(idx + 1))
        args.pop(idx)
        if not garmin_fit_override.exists():
            print(f"--garmin-fit file not found: {garmin_fit_override}")
            sys.exit(1)

    if len(args) < 1:
        print(__doc__)
        sys.exit(1)

    strava_id = int(args[0])
    garmin_id = int(args[1]) if len(args) > 1 and not garmin_fit_override else None
    output    = Path(args[2] if len(args) > 2 else args[1] if garmin_fit_override and len(args) > 1 else f"merged_{strava_id}.fit")

    log.info("Strava activity : %s", strava_id)
    if garmin_fit_override:
        log.info("Garmin FIT      : %s (local)", garmin_fit_override)
    else:
        log.info("Garmin activity : %s", garmin_id)
    log.info("Output          : %s", output)

    token = strava_access_token()
    icg_records = strava_fetch_streams(strava_id, token)

    if garmin_fit_override:
        garmin_fit = garmin_fit_override
    else:
        garmin_fit = output.with_suffix(".garmin_watch.fit")
        garmin_download_fit(garmin_id, garmin_fit)

    merge(garmin_fit, icg_records, output)
    log.info("Done. Upload %s to Garmin Connect manually.", output)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        log.exception("Error: %s", exc)
        sys.exit(1)
