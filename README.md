# spin-sync

Auto-syncs ICG IC7 spin workouts from Strava to Garmin Connect by merging
the bike's power/cadence data into the watch's FIT file, so Garmin correctly
calculates Training Effect and training load — no manual steps per workout.

## How it works

The sync runs on a schedule (~15 min after each class). It polls Strava for new ICG
activities, downloads the `.fit` file, finds the matching watch activity in Garmin Connect,
**merges** ICG power/cadence data into the Garmin watch file (preserving Training Effect
computed on-device), deletes duplicates from both platforms, and uploads the merged file
to Garmin Connect.

Result: one activity in Strava (original ICG recording) and one in Garmin Connect
(merged file with watch Training Effect + bike power/cadence). Zero manual steps per
workout.

See [docs/architecture.md](docs/architecture.md) for a full breakdown.

## Setup

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/spin-sync.git
cd spin-sync
uv sync
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env — see comments for where to find each value
```

### 3. Get your Strava refresh token (one-time)

```bash
uv run python scripts/strava_auth.py
```

A browser window will open. Approve access, then copy the printed refresh
token into `.env`.

### 4. Test manually

```bash
export $(grep -v '^#' .env | xargs)
uv run python src/sync.py
```

---

## Running automatically

### Option A — GitHub Actions (recommended, no always-on machine needed)

1. Push this repo to GitHub.
2. Add the following **repository secrets** (Settings → Secrets → Actions):

   | Secret | Value |
   |---|---|
   | `STRAVA_CLIENT_ID` | From your Strava API app |
   | `STRAVA_CLIENT_SECRET` | From your Strava API app |
   | `STRAVA_REFRESH_TOKEN` | From `strava_auth.py` |
   | `GARMIN_EMAIL` | Your Garmin Connect email |
   | `GARMIN_PASSWORD` | Your Garmin Connect password |
   | `GH_PAT` *(optional)* | Personal access token with `secrets:write` scope — allows auto-rotation of the Strava refresh token |

3. The workflow at `.github/workflows/spin-sync.yml` runs automatically on a
   schedule timed ~15 min after each class slot (see the workflow file for
   exact cron times).  You can also trigger it manually from the Actions tab.

### Option B — Local cron / launchd

```bash
chmod +x scripts/install_cron.sh
./scripts/install_cron.sh
```

Installs a launchd job (macOS) or crontab entries (Linux) timed ~15 min
after each class ends.  Logs go to `spin-sync.log` in the repo root.

To uninstall (macOS):

```bash
launchctl unload ~/Library/LaunchAgents/com.spinsync.agent.plist
rm ~/Library/LaunchAgents/com.spinsync.agent.plist
```

---

## Configuration reference

See `.env.example` for all options.  The most important ones:

| Variable | Description |
|---|---|
| `LOOKBACK_SECONDS` | How far back to look on the first run. Default: 6 hours. |
| *(activity types)* | Which Strava activity types trigger a sync. Hardcoded in `src/sync.py` (`VirtualRide`, `Ride`); edit that file to change. |

## Troubleshooting

**"No original file available"** — Strava only stores original .fit files for
activities that were uploaded as files.  If the ICG app syncs via the Strava
API (rather than a file upload), there may be no downloadable .fit.  In that
case you'll need to manually export from the ICG app and upload to Strava as
a file first.

**Training Effect not calculating** — The merge uses the Garmin watch file as
the base, which preserves Training Effect fields computed on-device. If
Training Effect is missing, confirm your watch was recording during the class.

**Duplicate activities in Garmin Connect** — The state file
(`~/.spin-sync-state.json`) tracks synced IDs.  If it gets deleted or reset,
the next run will look back `LOOKBACK_SECONDS` and may re-upload.  Delete the
duplicate in Garmin Connect and the state will prevent it happening again.

## License

MIT
