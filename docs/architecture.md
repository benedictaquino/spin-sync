# Architecture

spin-sync is a Python automation that runs on a schedule (GitHub Actions or local scheduler) and bridges ICG IC7 spin bike workouts between Strava and Garmin Connect. The goal is one clean activity in each platform: the original ICG recording on Strava, and a merged file on Garmin Connect that combines the watch's Training Effect data with the bike's power and cadence data.

## The problem it solves

The ICG IC7 bike syncs workouts to the ICG app, which uploads them to Strava as a `.fit` file with full power and cadence data. The bike has no native Garmin integration, so Training Effect and training load on the Garmin watch don't reflect the workout — unless the user also records an activity on the watch simultaneously.

Recording on both devices creates duplicates:
- **Strava** ends up with two activities: the ICG recording (power data) and a Garmin watch auto-sync (no power data).
- **Garmin Connect** ends up with one activity from the watch, but it has no power or cadence.

The manual workflow was: record a cardio activity on the watch → let Garmin compute Training Effect from HR → export the ICG `.fit` from Strava → merge it with the Garmin watch `.fit` using a tool like GPSBabel → delete the empty watch activity from Garmin Connect → upload the merged file. Several steps after every class.

## Automated flow

```
Spin class (ICG IC7 bike)
        │
        ▼  ICG app auto-syncs
     Strava ── ICG activity (power + cadence) ── kept as-is
        │
        │  Garmin watch also auto-syncs to Strava
        ▼
  Strava watch duplicate (no power) ── DELETED by script
        │
        ▼  spin-sync runs ~15 min after class ends
  ┌─────────────────────────────────────┐
  │           spin-sync                 │
  │                                     │
  │  1. Poll Strava for new ICG         │
  │     VirtualRide / Ride activity     │
  │  2. Download ICG .fit from Strava   │
  │  3. Delete Strava watch duplicate   │
  │  4. Find matching watch activity    │
  │     in Garmin Connect and download  │
  │     its .fit                        │
  │  5. Merge: inject ICG power/cadence │
  │     into Garmin watch file          │
  │  6. Delete original watch activity  │
  │     from Garmin Connect             │
  │  7. Upload merged .fit to Garmin    │
  │  8. Record Strava ID in state file  │
  └─────────────────────────────────────┘
        │
        ▼
  Strava: ICG activity (original, untouched)
  Garmin Connect: merged activity (watch Training Effect + ICG power/cadence)
```

## Code structure

```
src/
  sync.py        — orchestration: API calls, activity matching, flow control
  merge_fit.py   — FIT file merging logic
scripts/
  strava_auth.py — one-time OAuth flow to obtain the Strava refresh token
  install_cron.sh — installs a local launchd / crontab job
.github/workflows/
  spin-sync.yml  — GitHub Actions workflow (recommended deployment)
```

### `src/sync.py`

The main entry point. Responsible for:

- **Strava polling**: calls `GET /athlete/activities` with `after` set to the last run epoch (stored in state). Filters for `VirtualRide` and `Ride` activity types produced by the ICG app.
- **Strava duplicate deletion**: finds the Garmin watch's auto-synced activity by looking for a `Ride`, `VirtualRide`, or `workout` activity within `TIME_MATCH_TOLERANCE_S` (default 10 min) of the ICG start time, with zero or missing `average_watts` (the watch duplicate has no power data); activities with nonzero `average_watts` are excluded — this protects the ICG activity and any other power-enabled recording from being deleted.
- **Garmin activity matching**: queries Garmin Connect for `indoor_cycling`, `cardio`, or `cycling` activities by date (derived as `start_epoch - TIME_MATCH_TOLERANCE_S` in UTC — near-midnight activities may query the previous date), matches by timestamp proximity.
- **State management**: persists synced Strava activity IDs and the last-run epoch to `~/.spin-sync-state.json` so re-runs are safe and already-processed activities are skipped.
- **Lazy Garmin login**: the `garmin_client()` singleton logs in once per process and reuses the session.

### `src/merge_fit.py`

Merges the ICG power/cadence data into the Garmin watch `.fit` file. The Garmin file is the authoritative base — its `file_id`, `device_info`, Training Effect fields, lap structure, and event messages are all preserved. Lap and session summary fields are recalculated from the merged records. ICG data is overlaid second-by-second.

Two FIT libraries are used deliberately:
- **`fitparse`** reads the ICG file. It's more tolerant of non-standard vendor FIT files produced by the ICG app.
- **`fit_tool`** reads and rewrites the Garmin file. It preserves all message types faithfully, which is required to keep the Training Effect and device metadata intact.

The merge algorithm:
1. Parse all ICG `record` messages into `RecordSnapshot` objects (timestamp, power, cadence, distance), sorted by time.
2. Walk each message in the Garmin file. For `RecordMessage` entries, binary-search the ICG snapshot list for the nearest timestamp within 5 seconds (`MAX_INTERPOLATION_GAP_S`). If found, inject ICG power, cadence, and distance (only non-`None` values are written; if an ICG field is absent, the original Garmin value is preserved).
3. After processing all records, recalculate `LapMessage` and `SessionMessage` summary fields: `avg_power`, `max_power`, `normalized_power` (30-second rolling average NP calculation), `avg_cadence`, `max_cadence`, `total_distance`. Note: for multi-lap activities, all merged records are used for every lap's summary (not scoped to each lap's time window) — per-lap accuracy is a known future improvement.
4. Write the rebuilt file using `FitFileBuilder`.

### GitHub Actions workflow

The workflow (`.github/workflows/spin-sync.yml`) runs on a fixed schedule timed ~15 minutes after each spin class ends (two cron entries per class slot — one for EDT, one for EST — because GitHub Actions cron runs in UTC and does not adjust for daylight saving time; both entries fire every week, but the extra run is harmless since the state file prevents re-processing already-synced activities). State is persisted across runs using GitHub Actions cache keyed by `run_id`, with a `restore-keys` fallback to pick up the most recent prior state.

If the Strava refresh token rotates during a run, the new token is written to `.strava_refresh_token` and a post-run step updates the `STRAVA_REFRESH_TOKEN` repository secret automatically via `gh secret set` (requires a `GH_PAT` secret with `secrets:write` scope).

## Environment variables

| Variable | Purpose |
|---|---|
| `STRAVA_CLIENT_ID/SECRET/REFRESH_TOKEN` | Strava API credentials |
| `GARMIN_EMAIL/PASSWORD` | Garmin Connect login |
| `LOOKBACK_SECONDS` | How far back to look on first run (default 6h; GitHub Actions uses 2h) |
| `TIME_MATCH_TOLERANCE_S` | Max gap between ICG and watch start times (default 600s) |
| `STATE_FILE` | Path to state JSON (default `~/.spin-sync-state.json`) |

## Key design decisions

**Garmin file as merge base, not ICG file.** The Garmin watch `.fit` contains FirstBeat Training Effect fields computed on-device from heart rate. These are proprietary and cannot be reconstructed from the ICG file. By using the Garmin file as the base and injecting ICG fields into it, the merged activity retains Training Effect and updates training load on the watch correctly.

**Strava ICG activity is never modified.** The original ICG activity on Strava is left intact. Only the empty Garmin watch duplicate is deleted from Strava. This preserves kudos, segment efforts, and the activity's Strava ID.

**Uploaded files don't auto-sync from Garmin to Strava.** Garmin Connect only auto-syncs activities that originate from a physical device sync — programmatic API uploads are not forwarded to Strava. This is why the script deletes the Strava duplicate directly rather than relying on a re-sync.

**Idempotent runs.** The state file records every processed Strava activity ID. If the script runs multiple times (e.g., cron fires while a previous run is still in progress, or a run is retried after failure), already-synced activities are skipped. The Strava duplicate deletion is safe to call on already-deleted resources (404 responses are treated as success). Garmin activity deletion treats any failure as non-fatal — exceptions are caught, a warning is logged, and the run continues — which achieves the same idempotent effect.
