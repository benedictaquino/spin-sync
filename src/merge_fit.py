"""
merge_fit.py — Merge ICG IC7 power/cadence data into a Garmin watch FIT file.

Strategy:
  - The Garmin watch file is the authoritative base: its file_id, device_info,
    Training Effect fields, lap messages, and event messages are all preserved
    exactly as recorded.  Garmin Connect and the Epix Pro will recognise it as
    a native watch activity and compute training load normally.
  - The ICG file contributes three data streams, looked up by nearest timestamp:
      power   (watts)
      cadence (rpm)
      distance (metres) — used to update lap and session summaries too
  - Records from the Garmin file that fall outside the ICG recording window are
    left unchanged (ICG app sometimes starts/stops a few seconds off).
  - Lap and session summary fields (avg_power, max_power, total_distance, etc.)
    are recalculated from the merged record stream so that Garmin Connect
    displays accurate summary stats.

Dependencies: fitparse (pip install python-fitparse), fit-tool
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import fitparse
from fit_tool.fit_file import FitFile
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage

log = logging.getLogger("spin-sync.merge")

# ICG fields we want to inject into the Garmin file
ICG_FIELDS = ("power", "cadence", "distance")

# Maximum time gap (seconds) between a Garmin record timestamp and the nearest
# ICG record before we give up and leave the Garmin record unchanged.
MAX_INTERPOLATION_GAP_S = 5


@dataclass
class RecordSnapshot:
    """Lightweight snapshot of a single FIT record message."""
    timestamp_ms: int        # Unix milliseconds (ms since 1970-01-01 UTC)
    power:        Optional[int]   = None  # watts
    cadence:      Optional[int]   = None  # rpm
    distance:     Optional[float] = None  # metres


def _fit_epoch_offset_ms() -> int:
    """
    FIT timestamps are seconds since 1989-12-31 00:00:00 UTC.
    Return the offset so we can convert to Unix ms for easier arithmetic.
    """
    fit_epoch = datetime(1989, 12, 31, 0, 0, 0, tzinfo=timezone.utc)
    unix_epoch = datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    return int((fit_epoch - unix_epoch).total_seconds() * 1000)


_FIT_EPOCH_OFFSET_MS = _fit_epoch_offset_ms()


def _to_unix_ms(fit_timestamp_s: float) -> int:
    return int(fit_timestamp_s * 1000) + _FIT_EPOCH_OFFSET_MS


def _parse_icg_records(icg_path: Path) -> list[RecordSnapshot]:
    """
    Parse the ICG FIT file and return a list of RecordSnapshots sorted by time.
    Uses fitparse for reading since it handles a wider range of vendor FIT files.
    """
    snapshots: list[RecordSnapshot] = []
    fitfile = fitparse.FitFile(str(icg_path), check_crc=False)

    for msg in fitfile.get_messages("record"):
        fields = {d.name: d.value for d in msg if d.value is not None}
        ts = fields.get("timestamp")
        if ts is None:
            continue

        # fitparse returns datetime objects for timestamps
        if isinstance(ts, datetime):
            ts_ms = int(ts.timestamp() * 1000)
        else:
            ts_ms = _to_unix_ms(float(ts))

        snapshots.append(RecordSnapshot(
            timestamp_ms=ts_ms,
            power=fields.get("power"),
            cadence=fields.get("cadence") or fields.get("cadence_256"),
            distance=fields.get("distance"),
        ))

    snapshots.sort(key=lambda r: r.timestamp_ms)
    log.info("Parsed %d records from ICG file (%s)", len(snapshots), icg_path.name)
    return snapshots


def _nearest_icg(snapshots: list[RecordSnapshot], target_ms: int) -> Optional[RecordSnapshot]:
    """Binary-search for the ICG record closest in time to target_ms."""
    if not snapshots:
        return None

    lo, hi = 0, len(snapshots) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if snapshots[mid].timestamp_ms < target_ms:
            lo = mid + 1
        else:
            hi = mid

    # Check both neighbours and pick the closer one
    candidates = [snapshots[lo]]
    if lo > 0:
        candidates.append(snapshots[lo - 1])

    best = min(candidates, key=lambda r: abs(r.timestamp_ms - target_ms))
    gap_s = abs(best.timestamp_ms - target_ms) / 1000.0
    if gap_s > MAX_INTERPOLATION_GAP_S:
        return None
    return best


def _recalculate_summaries(records: list[dict]) -> dict:
    """
    Given a list of merged record field dicts, recalculate summary stats
    for use in Lap and Session messages.
    """
    powers    = [r["power"]    for r in records if r.get("power")    is not None]
    cadences  = [r["cadence"]  for r in records if r.get("cadence")  is not None]
    distances = [r["distance"] for r in records if r.get("distance") is not None]

    result: dict = {}
    if powers:
        result["avg_power"] = int(sum(powers) / len(powers))
        result["max_power"] = max(powers)
        result["normalized_power"] = _calc_np(powers)
    if cadences:
        result["avg_cadence"] = int(sum(cadences) / len(cadences))
        result["max_cadence"] = max(cadences)
    if distances:
        result["total_distance"] = distances[-1]  # cumulative, use last value

    return result


def _calc_np(power_series: list[int], window: int = 30) -> int:
    """
    Calculate Normalized Power (NP) from a 1-second power series.
    NP = (mean of 30-second rolling average raised to the 4th power) ^ 0.25
    """
    if len(power_series) < window:
        return int(sum(power_series) / len(power_series))

    rolling: list[float] = []
    for i in range(window - 1, len(power_series)):
        window_slice = power_series[i - window + 1 : i + 1]
        rolling.append(sum(window_slice) / window)

    mean_fourth = sum(r ** 4 for r in rolling) / len(rolling)
    return int(mean_fourth ** 0.25)


# All RecordMessage property names, used for field copying.
_RECORD_PROPERTIES = (
    "timestamp", "position_lat", "position_long", "altitude", "heart_rate",
    "cadence", "distance", "speed", "power", "grade", "resistance",
    "time_from_course", "cycle_length", "temperature", "speed_1s", "cycles",
    "total_cycles", "compressed_accumulated_power", "accumulated_power",
    "left_right_balance", "gps_accuracy", "vertical_speed", "calories",
    "vertical_oscillation", "stance_time_percent", "stance_time", "activity_type",
    "left_torque_effectiveness", "right_torque_effectiveness",
    "left_pedal_smoothness", "right_pedal_smoothness", "combined_pedal_smoothness",
    "time128", "stroke_type", "zone", "ball_speed", "cadence256",
    "fractional_cadence", "total_hemoglobin_conc", "total_hemoglobin_conc_min",
    "total_hemoglobin_conc_max", "saturated_hemoglobin_percent",
    "saturated_hemoglobin_percent_min", "saturated_hemoglobin_percent_max",
    "device_index", "enhanced_speed", "enhanced_altitude",
)


def _clone_record_with_icg(orig: RecordMessage, icg: RecordSnapshot) -> RecordMessage:
    """
    Return a new RecordMessage (all fields growable) with values copied from orig,
    then ICG power/cadence/distance injected.  Required because Garmin watch records
    are parsed with a fixed definition that marks power/cadence as non-growable size-0
    fields — you cannot set a value on them directly.
    """
    new_msg = RecordMessage()
    for attr in _RECORD_PROPERTIES:
        try:
            val = getattr(orig, attr)
            if val is not None:
                setattr(new_msg, attr, val)
        except Exception:
            pass
    if icg.power    is not None: new_msg.power    = icg.power
    if icg.cadence  is not None: new_msg.cadence  = icg.cadence
    if icg.distance is not None: new_msg.distance = icg.distance
    return new_msg


def merge(garmin_path: Path, icg_records: list[RecordSnapshot], output_path: Path) -> None:
    """
    Merge ICG power/cadence data into the Garmin watch FIT file.

    garmin_path : .fit exported from the Garmin watch (Indoor Cycling activity)
    icg_records : pre-parsed list of RecordSnapshot (from Strava Streams or FIT file)
    output_path : destination for the merged .fit file
    """
    if not icg_records:
        log.warning("No ICG records provided — output will be a copy of Garmin file.")
        output_path.write_bytes(garmin_path.read_bytes())
        return

    icg_start_ms = icg_records[0].timestamp_ms
    icg_end_ms   = icg_records[-1].timestamp_ms
    log.info(
        "ICG recording window: %s → %s (%.1f min)",
        datetime.fromtimestamp(icg_start_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S"),
        datetime.fromtimestamp(icg_end_ms   / 1000, tz=timezone.utc).strftime("%H:%M:%S"),
        (icg_end_ms - icg_start_ms) / 60000,
    )

    # --- Read the Garmin FIT file using fit_tool (preserves all message types) ---
    garmin_fit  = FitFile.from_file(str(garmin_path))
    builder     = FitFileBuilder(auto_define=True)

    injected = 0
    merged_record_fields: list[dict] = []

    for fit_record in garmin_fit.records:
        msg = fit_record.message

        if isinstance(msg, RecordMessage):
            # fit_tool already returns Unix ms — no epoch conversion needed
            ts_ms = msg.timestamp or 0

            icg = _nearest_icg(icg_records, ts_ms)
            if icg is not None:
                # Garmin watch records are parsed with a fixed definition that
                # marks power/cadence/distance as non-growable.  We must clone
                # into a fresh RecordMessage (all fields growable) before setting.
                msg = _clone_record_with_icg(msg, icg)
                injected += 1

            # Track merged values for summary recalculation
            merged_record_fields.append({
                "power":    msg.power,
                "cadence":  msg.cadence,
                "distance": msg.distance,
            })

        elif isinstance(msg, LapMessage):
            # Recalculate lap summaries from merged records that fall within
            # this lap's time window.  For simplicity, use all merged records
            # (for single-lap indoor activities this is exact; for multi-lap
            # workouts you'd need to track per-lap windows — a future improvement).
            summaries = _recalculate_summaries(merged_record_fields)
            for attr, value in summaries.items():
                try:
                    setattr(msg, attr, value)
                except AttributeError:
                    pass  # Not all lap messages have every field

        elif isinstance(msg, SessionMessage):
            summaries = _recalculate_summaries(merged_record_fields)
            for attr, value in summaries.items():
                try:
                    setattr(msg, attr, value)
                except AttributeError:
                    pass

        builder.add(msg)

    builder.build().to_file(str(output_path))

    total_garmin_records = len(merged_record_fields)
    log.info(
        "Merge complete: injected ICG data into %d/%d Garmin records (%.0f%%). Output: %s",
        injected,
        total_garmin_records,
        100 * injected / total_garmin_records if total_garmin_records else 0,
        output_path,
    )

    if injected < total_garmin_records * 0.5:
        log.warning(
            "Less than 50%% of records were matched. Check that both files "
            "cover the same time window (timestamps may be offset by timezone)."
        )
