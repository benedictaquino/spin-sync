# Bug: Duplicate merge() call

## Location

`src/sync.py:507-508`

## Description

`merge(garmin_fit, icg_records, merged_fit)` is called twice in succession on the same arguments. The second call overwrites the merged file with an identical result.

```python
# 5. Merge ICG power/cadence into Garmin watch file
try:
    merge(garmin_fit, icg_records, merged_fit)
    merge(garmin_fit, icg_records, merged_fit)  # <-- duplicate, remove this line
```

## Impact

Low — the merge is idempotent so the output is correct, but it doubles the merge time for each sync.

## Fix

Delete line 508 (the second `merge()` call).
