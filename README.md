# spin-sync

Auto-syncs ICG IC7 spin workouts from Strava to Garmin Connect, with device
metadata patching so Garmin correctly calculates Training Effect and training
load — no manual cardio activity needed.

## How it works

```
Spin class (ICG IC7)
      │
      ▼  (automatic via ICG app)
   Strava
      │
      ▼  spin-sync detects new VirtualRide / indoor cycling activity
      │  ├─ Downloads original .fit file from Strava
      │  ├─ Patches device metadata to match your Garmin watch
      │  └─ Uploads to Garmin Connect
      ▼
Garmin Connect  ←  full power data + training load / Training Effect
```

## Setup

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/spin-sync.git
cd spin-sync
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env — see comments for where to find each value
```

### 3. Get your Strava refresh token (one-time)

```bash
python scripts/strava_auth.py
```

A browser window will open. Approve access, then copy the printed refresh
token into `.env`.

### 4. Find your Garmin Unit ID

In the Garmin Connect mobile app:
**Devices → [Your Device] → System → About**

Copy the **Unit ID** (not the serial number on the box) into `.env`.

### 5. Test manually

```bash
source .env   # or: export $(grep -v '^#' .env | xargs)
python src/sync.py
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
   | `GARMIN_DEVICE_UNIT_ID` | Your watch Unit ID |
   | `GARMIN_PRODUCT_ID` | Your watch product ID (see `.env.example`) |
   | `GH_PAT` *(optional)* | Personal access token with `secrets:write` scope — allows auto-rotation of the Strava refresh token |

3. The workflow at `.github/workflows/spin-sync.yml` runs every **30 minutes**
   automatically.  You can also trigger it manually from the Actions tab.

### Option B — Local cron / launchd

```bash
chmod +x scripts/install_cron.sh
./scripts/install_cron.sh
```

Installs a launchd job (macOS) or crontab entry (Linux) that runs every
30 minutes.  Logs go to `spin-sync.log` in the repo root.

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
| `GARMIN_PRODUCT_ID` | FIT product ID for your watch model. Controls Training Effect. Defaults to `3290` (Forerunner 965). |
| `LOOKBACK_SECONDS` | How far back to look on the first run. Default: 6 hours. |
| `TARGET_ACTIVITY_TYPES` | Edit `src/sync.py` to change which Strava activity types trigger a sync. Defaults: `VirtualRide`, `Ride`. |

## Troubleshooting

**"No original file available"** — Strava only stores original .fit files for
activities that were uploaded as files.  If the ICG app syncs via the Strava
API (rather than a file upload), there may be no downloadable .fit.  In that
case you'll need to manually export from the ICG app and upload to Strava as
a file first.

**Training Effect not calculating** — Make sure `GARMIN_DEVICE_UNIT_ID` is
set correctly and that `GARMIN_PRODUCT_ID` matches your actual watch model.

**Duplicate activities in Garmin Connect** — The state file
(`~/.spin-sync-state.json`) tracks synced IDs.  If it gets deleted or reset,
the next run will look back `LOOKBACK_SECONDS` and may re-upload.  Delete the
duplicate in Garmin Connect and the state will prevent it happening again.

## License

MIT
