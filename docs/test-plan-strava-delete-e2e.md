# Test Plan: Strava Delete + Full End-to-End Sync

## Context

A Strava delete call has failed previously, likely returning an HTTP error from `DELETE /api/v3/activities/{id}`. The most common cause is that the OAuth token was granted without `activity:write` scope (Strava lets users deselect scopes during authorization). Today's 12 PM spin class is an opportunity to validate the full sync flow end-to-end with a real double-tracked workout (ICG + Garmin Cardio).

## Pre-Class Steps (Before 12 PM)

### 1. Fix the Strava OAuth token (the delete issue)

The token refresh endpoint (`POST /oauth/token`) preserves the scopes from the *original* authorization. If the original auth didn't include `activity:write`, refreshed tokens won't have it either. **You must re-authorize from scratch:**

```bash
# Re-run the auth flow — approve ALL requested scopes in the browser
uv run python scripts/strava_auth.py
```

When the browser opens, **verify the Strava consent screen shows both "View data about your activities" AND "Create, edit, and delete activities"**. If only read is shown, the scope request may be wrong (though the code correctly requests `activity:read_all,activity:write`).

Update `.env` with the new `STRAVA_REFRESH_TOKEN`.

### 2. Verify the token has write scope

Quick smoke test — call the Strava API to confirm the token's scopes:

```bash
export $(grep -v '^#' .env | xargs)
python3 -c "
import requests
resp = requests.post('https://www.strava.com/oauth/token', data={
    'client_id': '$STRAVA_CLIENT_ID',
    'client_secret': '$STRAVA_CLIENT_SECRET',
    'grant_type': 'refresh_token',
    'refresh_token': '$STRAVA_REFRESH_TOKEN',
}, timeout=15)
data = resp.json()
print('Access token scopes:', data.get('scope', 'NOT RETURNED'))
print('Token starts with:', data['access_token'][:10] + '...')
"
```

**Expected:** scope includes `activity:write`. If it only shows `read` or `read_all`, the re-auth didn't stick.

### 3. Verify Garmin session is still valid

```bash
export $(grep -v '^#' .env | xargs)
python3 -c "
from src.sync import garmin_session
gs = garmin_session()
acts = gs.get_activities_by_date('$(date +%Y-%m-%d)', '$(date +%Y-%m-%d)')
print(f'Garmin session OK — found {len(acts)} activities today')
"
```

If this fails with 401/403, re-run `uv run python scripts/garmin_auth.py`.

### 4. Clear state for a clean test

```bash
# Back up current state, then reset so the test workout is picked up fresh
cp ~/.spin-sync-state.json ~/.spin-sync-state.json.bak
echo '{"synced_ids": [], "last_run_epoch": 0}' > ~/.spin-sync-state.json
```

Set `LOOKBACK_SECONDS` to something small (e.g., 2 hours) so only today's activity is picked up:

```bash
export LOOKBACK_SECONDS=7200
```

## During Class (12 PM)

1. **Start ICG workout on the bike** — this uploads to Strava via the ICG app
2. **Start a "Cardio" activity on your Garmin watch** at roughly the same time
3. Let both run for the full class

The Garmin watch will auto-sync to both Garmin Connect and Strava, creating the duplicate scenario the sync tool is designed to handle.

## Post-Class Testing (~12:45 PM, after both activities have synced)

### Step A: Verify both activities appear on Strava

```bash
export $(grep -v '^#' .env | xargs)
python3 -c "
import requests, os, time
resp = requests.post('https://www.strava.com/oauth/token', data={
    'client_id': os.environ['STRAVA_CLIENT_ID'],
    'client_secret': os.environ['STRAVA_CLIENT_SECRET'],
    'grant_type': 'refresh_token',
    'refresh_token': os.environ['STRAVA_REFRESH_TOKEN'],
}, timeout=15)
token = resp.json()['access_token']
acts = requests.get('https://www.strava.com/api/v3/athlete/activities',
    headers={'Authorization': f'Bearer {token}'},
    params={'after': int(time.time()) - 7200, 'per_page': 10}, timeout=15).json()
for a in acts:
    print(f\"  {a['id']}  {a['type']:15s}  avg_watts={a.get('average_watts', 'N/A'):>6}  {a['name']}\")
"
```

**Expected:** Two activities — one with power (ICG, `VirtualRide`/`Ride`) and one without (watch, `workout`/`Ride` with `average_watts=0`).

### Step B: Verify the watch activity also appears in Garmin Connect

Use step 3's script but check for a `cardio` type activity starting around 12 PM.

### Step C: Test delete in isolation first

Before running the full sync, test just the delete to confirm it works:

```bash
export $(grep -v '^#' .env | xargs)
# DON'T actually run this yet — just prepare the activity ID from Step A
# The watch duplicate's Strava ID from the listing above
# python3 -c "
# from src.sync import strava_refresh_access_token, strava_delete_activity
# token = strava_refresh_access_token()
# result = strava_delete_activity(WATCH_ACTIVITY_ID_HERE, token)
# print('Delete succeeded:', result)
# "
```

If this returns `True` (HTTP 204), the scope issue is fixed. If it fails, check the HTTP status:
- **401/403** → scope still wrong, re-authorize
- **404** → activity already gone
- **Other** → investigate

### Step D: Run the full sync

```bash
export $(grep -v '^#' .env | xargs)
export LOOKBACK_SECONDS=7200
uv run python src/sync.py 2>&1 | tee /tmp/spin-sync-test.log
```

### Step E: Verify results

Check these post-sync:

1. **Strava:** Only the ICG activity remains (watch duplicate deleted)
2. **Garmin Connect:** The old empty Cardio activity is gone, replaced by a merged activity with power data + Training Effect
3. **State file:** `~/.spin-sync-state.json` contains the ICG activity's Strava ID in `synced_ids`
4. **Merged FIT quality:** Open the new Garmin activity in Garmin Connect and verify:
   - Power data is present (avg power, max power graphs)
   - Heart rate is present (from watch)
   - Training Effect is calculated (aerobic + anaerobic)
   - Lap summaries show power

### Step F: Restore state

```bash
cp ~/.spin-sync-state.json.bak ~/.spin-sync-state.json
```

(Or keep the new state if you want to prevent re-processing.)

## Key Risks / Watch Out For

| Risk | Mitigation |
|---|---|
| Garmin "Cardio" activity type not matched | Already handled — `GARMIN_INDOOR_ACTIVITY_TYPES` includes `"cardio"` (line 66) |
| Watch activity hasn't synced to Strava yet when sync runs | Wait 15+ min after class; check Step A first |
| Garmin session expired | Pre-check in step 3; re-auth if needed |
| ICG app slow to upload | Wait for it to appear on Strava before running sync |

## Related

- See `docs/bug-duplicate-merge-call.md` for a separate bug found during this review
