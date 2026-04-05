# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

spin-sync auto-syncs ICG IC7 spin bike workouts from Strava to Garmin Connect. The core problem: the ICG app uploads power/cadence data to Strava as a FIT file, but Garmin Connect also auto-syncs an empty watch recording of the same workout (no power data, so no Training Effect). This tool merges the two into a single Garmin activity with full power data and correct Training Effect.

**Flow per workout:**
1. Poll Strava for new `VirtualRide`/`Ride` activities (ICG source)
2. Download the original ICG `.fit` from Strava
3. Find and delete the empty Garmin watch duplicate on Strava
4. Find the matching watch activity in Garmin Connect and download its `.fit`
5. Merge ICG power/cadence into the Garmin watch `.fit` (preserving HR, Training Effect metadata)
6. Delete the original empty watch activity from Garmin Connect
7. Upload the merged `.fit` to Garmin Connect
8. Record the Strava activity ID in `~/.spin-sync-state.json` to prevent re-processing

## Setup and running

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with credentials

# One-time: get Strava OAuth refresh token
python scripts/strava_auth.py

# Run manually (requires env vars to be set)
export $(grep -v '^#' .env | xargs)
python src/sync.py
```

## Architecture

Two source files in `src/`:

**`src/sync.py`** — orchestration layer. Handles all API calls (Strava REST, Garmin Connect via `garminconnect` library), state management, and the 8-step sync flow. State is persisted to `~/.spin-sync-state.json` (configurable via `STATE_FILE` env var). Activity matching uses `TIME_MATCH_TOLERANCE_S` (default 10 min) to correlate ICG and watch activities by timestamp.

**`src/merge_fit.py`** — FIT file merging. Uses two different libraries:
- `fitparse` for reading ICG FIT files (handles non-standard vendor files)
- `fit_tool` for reading/writing the Garmin FIT file (preserves all message types including device metadata that drives Training Effect)

The merge does a nearest-neighbor lookup (binary search, max 5s gap) to inject ICG power/cadence/distance into each Garmin record message. Lap and session summaries are recalculated from merged records, including Normalized Power (30-second rolling average).

**`scripts/strava_auth.py`** — one-time OAuth flow to obtain the Strava refresh token.

## Key environment variables

| Variable | Purpose |
|---|---|
| `STRAVA_CLIENT_ID/SECRET/REFRESH_TOKEN` | Strava API credentials |
| `GARMIN_EMAIL/PASSWORD` | Garmin Connect login |
| `LOOKBACK_SECONDS` | How far back to look on first run (default 6h; GitHub Actions uses 2h) |
| `TIME_MATCH_TOLERANCE_S` | Max gap between ICG and watch start times (default 600s) |
| `STATE_FILE` | Path to state JSON (default `~/.spin-sync-state.json`) |

## Automation

**GitHub Actions** (`.github/workflows/spin-sync.yml`): runs on a schedule timed ~15 min after each spin class ends. State is persisted across runs using GitHub Actions cache. Strava refresh token rotation is handled automatically if `GH_PAT` secret has `secrets:write` scope.

**Local** (`scripts/install_cron.sh`): installs a launchd job (macOS) or crontab entries (Linux) timed ~15 min after each class ends.

## Dependencies

- `garminconnect` — unofficial Garmin Connect API client
- `fit-tool` — FIT file read/write (preserves all message types)
- `python-fitparse` — FIT file reading (better vendor file compatibility)
- `requests` — Strava REST API calls
- `python-dotenv` — loads `.env` file during the one-time Strava auth setup
