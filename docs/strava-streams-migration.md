# Technical Design: Replace FIT Download with Strava Streams API

**Date:** 2026-04-05
**Status:** Implemented

## Problem

Strava's `/activities/{id}/export_original` is a **web-only** endpoint that requires session cookies, not OAuth Bearer tokens. Our `strava_download_fit()` function can never work with API authentication -- it always redirects to `/login`. Confirmed via Strava's official API docs: there is no public API endpoint for downloading original FIT files.

This makes the current approach incompatible with both automated runs (GitHub Actions) and local OAuth-based usage.

## Solution

Use the official `GET /api/v3/activities/{id}/streams` endpoint to fetch second-by-second power/cadence/time/distance data as JSON, then convert that into the `RecordSnapshot` list that the merge logic already expects.

### Strava Streams API

**Endpoint:** `GET /api/v3/activities/{id}/streams`

**Parameters:**
- `keys` (required): comma-separated stream types -- `watts,cadence,time,distance`
- `key_by_type` (required): must be `true`

**Auth:** Bearer token with `activity:read_all` scope (already requested by `strava_auth.py`).

**Response format** (keyed by type):
```json
{
  "time":     {"data": [0, 1, 2, ...], "series_type": "distance", "original_size": N, "resolution": "high"},
  "watts":    {"data": [150, 152, ...], ...},
  "cadence":  {"data": [80, 82, ...], ...},
  "distance": {"data": [0.0, 3.2, ...], ...}
}
```

The `time` stream gives seconds elapsed from activity start. Combined with the activity's `start_date`, this yields absolute Unix timestamps for each sample.

---

## Changes

### 1. `src/sync.py` -- Replace `strava_download_fit` with `strava_fetch_icg_streams`

**Delete:** `strava_download_fit()` (lines 115-139) and its `zipfile`/`io` imports.

**Add:** New function:
```python
def strava_fetch_icg_streams(
    activity_id: int, access_token: str, start_epoch: int,
) -> list[RecordSnapshot] | None:
    """Fetch power/cadence/distance/time streams from Strava API.
    Returns list of RecordSnapshot, or None on failure."""
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

    # time stream values are seconds from activity start
    start_ms = start_epoch * 1000
    records = []
    for i, t in enumerate(time_data):
        records.append(RecordSnapshot(
            timestamp_ms=start_ms + int(t * 1000),
            power=watts[i]    if i < len(watts)    else None,
            cadence=cadence[i] if i < len(cadence) else None,
            distance=distance[i] if i < len(distance) else None,
        ))
    return records
```

**Update call site** in `run()`: replace the `strava_download_fit` call with `strava_fetch_icg_streams`, passing `start_epoch` from `strava_activity_start_epoch(icg_activity)`.

**Add import:** `from merge_fit import merge, RecordSnapshot`

### 2. `src/merge_fit.py` -- Accept pre-parsed records in `merge()`

**Change `merge()` signature:**
```python
# Before:
def merge(garmin_path: Path, icg_path: Path, output_path: Path) -> None:

# After:
def merge(garmin_path: Path, icg_records: list[RecordSnapshot], output_path: Path) -> None:
```

- Remove the `_parse_icg_records(icg_path)` call -- records arrive pre-parsed from the caller
- Update the empty-records check to work with the passed-in list
- Update docstring
- `_parse_icg_records()` can be deleted (only used for ICG FIT file reading)

### 3. `src/sync.py` -- Update `merge()` call site

```python
# Before:
merge(garmin_fit, icg_fit, merged_fit)

# After:
merge(garmin_fit, icg_records, merged_fit)
```

The `icg_fit` temp file is no longer needed -- remove from tmpdir setup.

### 4. Cleanup

- Remove `import zipfile` and `import io` from sync.py (only used by deleted function)
- Remove `icg_fit = tmp / "icg.fit"` from the tmpdir block
- `fitparse` dependency in `requirements.txt` can be removed (only used for ICG FIT reading)

---

## Files modified

| File | What changes |
|---|---|
| `src/sync.py` | Delete `strava_download_fit`, add `strava_fetch_icg_streams`, update `run()` call sites, add `RecordSnapshot` import, remove `zipfile`/`io` imports |
| `src/merge_fit.py` | Change `merge()` to accept `list[RecordSnapshot]` instead of `icg_path: Path`, remove internal `_parse_icg_records` call |
| `requirements.txt` | Remove `python-fitparse` (no longer needed) |

---

## Verification

```bash
rm ~/.spin-sync-state.json
export LOOKBACK_SECONDS=86400
python src/sync.py
```

Expected:
1. "Found N total activities, M new ICG candidate(s)" -- same as before
2. No more redirect warnings -- streams are fetched via official API
3. Success line: `'<name>' done -- Strava has ICG original, Garmin Connect has merged file with Training Effect + power.`
4. Garmin Connect shows power/cadence data and Training Effect on the activity
5. `~/.spin-sync-state.json` contains the synced activity ID
