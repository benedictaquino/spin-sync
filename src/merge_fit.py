"""
merge_fit.py — Merge ICG IC7 power/cadence data into a Garmin watch FIT file.

Strategy:
  - The Garmin watch file is the authoritative base: every byte is preserved
    except the RecordMessage definitions (global_id=20), which get power/cadence/
    distance field definitions appended, and their corresponding data records,
    which get the injected values appended.
  - All other message types (FileId, DeviceInfo, Lap, Session, Event, …) are
    passed through byte-for-byte so Training Effect and device metadata are intact.
  - The ICG streams (from Strava) contribute three data fields looked up by
    nearest timestamp (binary search, max 5 s gap):
        power    (watts)
        cadence  (rpm)
        distance (metres, written as cm in the FIT uint32 field)
  - Records outside the ICG window get FIT invalid-value placeholders so the
    merged file has a single consistent RecordMessage definition throughout.

Dependencies: fitparse (reading ICG .fit files), fit-tool (CRC utility only)
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import fitparse
from fit_tool.utils.crc import crc16 as _fit_crc16

log = logging.getLogger("spin-sync.merge")

# Maximum time gap (seconds) between a Garmin record timestamp and the nearest
# ICG record before we fall back to FIT invalid-value placeholders.
MAX_INTERPOLATION_GAP_S = 5

# FIT global message numbers.
_GLOBAL_ID_RECORD  = 20
_GLOBAL_ID_LAP     = 19
_GLOBAL_ID_SESSION = 18

# FIT field numbers inside a record message.
_FID_TIMESTAMP     = 253
_FID_POWER         = 7    # uint16, watts, scale=1
_FID_CADENCE       = 4    # uint8,  rpm,   scale=1
_FID_DISTANCE      = 5    # uint32, m,     scale=100  (value = metres * 100)

# FIT field number for total_distance in Lap and Session messages.
_FID_TOTAL_DISTANCE = 9   # uint32, m, scale=100

# FIT base-type bytes (written into the DefinitionMessage field definitions).
_BT_UINT8  = 0x02
_BT_UINT16 = 0x84
_BT_UINT32 = 0x86

# FIT invalid sentinel values.
_INVALID_UINT8  = 0xFF
_INVALID_UINT16 = 0xFFFF
_INVALID_UINT32 = 0xFFFFFFFF

# Seconds from Unix epoch (1970-01-01) to FIT epoch (1989-12-31).
_FIT_EPOCH_OFFSET_S = 631_065_600


@dataclass
class RecordSnapshot:
    """Lightweight snapshot of a single FIT record message."""
    timestamp_ms: int             # Unix milliseconds (ms since 1970-01-01 UTC)
    power:        Optional[int]   = None  # watts
    cadence:      Optional[int]   = None  # rpm
    distance:     Optional[float] = None  # metres


# ---------------------------------------------------------------------------
# ICG FIT-file parser (used when the source is a downloaded .fit, not Strava)
# ---------------------------------------------------------------------------

def _fit_epoch_offset_ms() -> int:
    fit_epoch  = datetime(1989, 12, 31, 0, 0, 0, tzinfo=timezone.utc)
    unix_epoch = datetime(1970,  1,  1, 0, 0, 0, tzinfo=timezone.utc)
    return int((fit_epoch - unix_epoch).total_seconds() * 1000)


_FIT_EPOCH_OFFSET_MS = _fit_epoch_offset_ms()


def _to_unix_ms(fit_timestamp_s: float) -> int:
    return int(fit_timestamp_s * 1000) + _FIT_EPOCH_OFFSET_MS


def _parse_icg_records(icg_path: Path) -> list[RecordSnapshot]:
    """
    Parse an ICG FIT file and return RecordSnapshots sorted by time.
    Uses fitparse for broad vendor-FIT compatibility.
    """
    snapshots: list[RecordSnapshot] = []
    fitfile = fitparse.FitFile(str(icg_path), check_crc=False)

    for msg in fitfile.get_messages("record"):
        fields = {d.name: d.value for d in msg if d.value is not None}
        ts = fields.get("timestamp")
        if ts is None:
            continue
        ts_ms = int(ts.timestamp() * 1000) if isinstance(ts, datetime) else _to_unix_ms(float(ts))
        snapshots.append(RecordSnapshot(
            timestamp_ms=ts_ms,
            power=fields.get("power"),
            cadence=fields.get("cadence") or fields.get("cadence_256"),
            distance=fields.get("distance"),
        ))

    snapshots.sort(key=lambda r: r.timestamp_ms)
    log.info("Parsed %d records from ICG file (%s)", len(snapshots), icg_path.name)
    return snapshots


# ---------------------------------------------------------------------------
# ICG nearest-record lookup
# ---------------------------------------------------------------------------

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

    candidates = [snapshots[lo]]
    if lo > 0:
        candidates.append(snapshots[lo - 1])

    best = min(candidates, key=lambda r: abs(r.timestamp_ms - target_ms))
    if abs(best.timestamp_ms - target_ms) / 1000.0 > MAX_INTERPOLATION_GAP_S:
        return None
    return best


# ---------------------------------------------------------------------------
# Binary-level FIT merge
# ---------------------------------------------------------------------------

def merge(garmin_path: Path, icg_records: list[RecordSnapshot], output_path: Path) -> None:
    """
    Merge ICG power/cadence/distance into the Garmin watch FIT file at the
    binary level.  The original file bytes are preserved exactly except:

      • Every RecordMessage DefinitionMessage gets power/cadence/distance
        field definitions appended (if not already present).
      • Every RecordMessage data record gets the corresponding injected bytes
        appended (FIT invalid-value placeholders when outside the ICG window).

    garmin_path : .fit exported from the Garmin watch (Indoor Cycling activity)
    icg_records : pre-parsed list of RecordSnapshot (from Strava Streams or FIT)
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

    raw = garmin_path.read_bytes()

    # --- Parse FIT file header ---
    header_size  = raw[0]
    records_size = struct.unpack_from("<I", raw, 4)[0]
    records_end  = header_size + records_size

    # definitions[local_id] tracks the parsed definition for each local message type.
    # Each entry: {global_id, endian, fields, data_size, ts_field, extra_fields, …}
    definitions: dict[int, dict] = {}

    # Total distance (in FIT uint32 units = metres × 100) to write into Lap/Session.
    total_distance_raw = 0
    last_dist = next((r.distance for r in reversed(icg_records) if r.distance is not None), None)
    if last_dist is not None:
        total_distance_raw = int(last_dist * 100)

    new_records = bytearray()
    injected     = 0
    total_rec    = 0

    offset = header_size
    while offset < records_end:
        hb = raw[offset]

        # ── Compressed-timestamp record (bit 7 = 1) ─────────────────────────
        if hb & 0x80:
            local_id = (hb >> 5) & 0x03
            d = definitions.get(local_id)
            if d is None:
                raise RuntimeError(
                    f"Compressed-timestamp record at {offset:#x} references "
                    f"undefined local_id={local_id}"
                )
            # Compressed-timestamp records omit the timestamp field from the
            # data payload; all other fields are present as normal.
            ct_data_size = d["data_size"] - (d["ts_field"]["size"] if d["ts_field"] else 0)
            if d["global_id"] == _GLOBAL_ID_RECORD and (d["extra_fields"] or d["overwrite_fields"]):
                # Can't look up ICG data without a full timestamp; use invalids
                # for appended fields and leave overwrite fields unchanged.
                new_records += bytes(raw[offset: offset + 1 + ct_data_size])
                new_records += _invalid_extra(d["extra_fields"], d["endian"])
            else:
                new_records += bytes(raw[offset: offset + 1 + ct_data_size])
            offset += 1 + ct_data_size
            continue

        is_def   = bool(hb & 0x40)
        has_dev  = bool(hb & 0x20) and is_def
        local_id = hb & 0x0F

        # ── Definition record ────────────────────────────────────────────────
        if is_def:
            # Layout after the header byte:
            #   [0]     reserved
            #   [1]     arch (0=LE, 1=BE)
            #   [2-3]   global message number
            #   [4]     number of fields
            #   [5 …]   field definitions (3 bytes each)
            #   [+dev]  optional developer-field section
            base = offset + 1   # points at reserved byte
            arch      = raw[base + 1]
            endian    = ">" if arch else "<"
            global_id = struct.unpack_from(endian + "H", raw, base + 2)[0]
            num_flds  = raw[base + 4]

            fields    = []
            data_size = 0
            ts_field  = None
            for i in range(num_flds):
                fp  = base + 5 + i * 3
                fid, fsz, fbt = raw[fp], raw[fp + 1], raw[fp + 2]
                entry = {"id": fid, "size": fsz, "base_type": fbt,
                         "data_offset": data_size}
                fields.append(entry)
                if fid == _FID_TIMESTAMP:
                    ts_field = entry
                data_size += fsz

            # Developer-field section (if present)
            def_body_len = 5 + num_flds * 3
            dev_bytes    = b""
            if has_dev:
                num_dev   = raw[base + def_body_len]
                dev_bytes = bytes(raw[base + def_body_len:
                                      base + def_body_len + 1 + num_dev * 3])
                for i in range(num_dev):
                    dp = base + def_body_len + 1 + i * 3
                    data_size += raw[dp + 1]
                def_body_len += 1 + num_dev * 3

            total_def_size = 1 + def_body_len   # header byte + body

            # Determine which ICG fields to inject for RecordMessages.
            # Fields already in the definition → overwrite their bytes in place.
            # Fields not in the definition → append to the definition and data.
            extra_fields:     list[dict] = []   # to be appended
            overwrite_fields: list[dict] = []   # to be patched in the existing data bytes
            if global_id == _GLOBAL_ID_RECORD:
                existing_by_id = {f["id"]: f for f in fields}
                for fid, fsz, fbt in [
                    (_FID_POWER,    2, _BT_UINT16),
                    (_FID_CADENCE,  1, _BT_UINT8),
                    (_FID_DISTANCE, 4, _BT_UINT32),
                ]:
                    if fid in existing_by_id:
                        overwrite_fields.append(existing_by_id[fid])
                    else:
                        extra_fields.append({"id": fid, "size": fsz, "base_type": fbt})

            # For Lap/Session: locate the total_distance field so we can patch it.
            total_dist_field = None
            if global_id in (_GLOBAL_ID_LAP, _GLOBAL_ID_SESSION):
                total_dist_field = next((f for f in fields if f["id"] == _FID_TOTAL_DISTANCE), None)

            definitions[local_id] = {
                "global_id":         global_id,
                "endian":            endian,
                "fields":            fields,
                "data_size":         data_size,
                "ts_field":          ts_field,
                "extra_fields":      extra_fields,
                "overwrite_fields":  overwrite_fields,
                "total_dist_field":  total_dist_field,
            }

            if extra_fields:
                # Emit a modified definition with the extra field defs appended.
                new_num = num_flds + len(extra_fields)
                new_body = (
                    bytes([0, arch])
                    + struct.pack(endian + "H", global_id)
                    + bytes([new_num])
                )
                for f in fields:
                    new_body += bytes([f["id"], f["size"], f["base_type"]])
                for ef in extra_fields:
                    new_body += bytes([ef["id"], ef["size"], ef["base_type"]])
                new_body += dev_bytes
                new_records += bytes([hb]) + new_body
            else:
                # Pass through unchanged.
                new_records += bytes(raw[offset: offset + total_def_size])

            offset += total_def_size

        # ── Data record ──────────────────────────────────────────────────────
        else:
            d = definitions.get(local_id)
            if d is None:
                raise RuntimeError(
                    f"Data record at {offset:#x} references undefined local_id={local_id}"
                )
            data_size  = d["data_size"]
            total_size = 1 + data_size

            if d["global_id"] == _GLOBAL_ID_RECORD and (d["extra_fields"] or d["overwrite_fields"]):
                total_rec += 1
                endian          = d["endian"]
                ts_field        = d["ts_field"]
                overwrite_fields = d["overwrite_fields"]

                # Extract FIT timestamp → Unix ms.
                icg = None
                if ts_field:
                    ts_off  = offset + 1 + ts_field["data_offset"]
                    fit_ts  = struct.unpack_from(endian + "I", raw, ts_off)[0]
                    unix_ms = (fit_ts + _FIT_EPOCH_OFFSET_S) * 1000
                    icg     = _nearest_icg(icg_records, unix_ms)

                # Start with a mutable copy of the original record bytes so we
                # can patch overwrite_fields in place before appending extra_fields.
                rec = bytearray(raw[offset: offset + total_size])
                _patch_overwrite(rec, overwrite_fields, endian, icg)
                new_records += bytes(rec) + _encode_extra(d["extra_fields"], endian, icg)
                if icg is not None:
                    injected += 1
            elif (d["global_id"] in (_GLOBAL_ID_LAP, _GLOBAL_ID_SESSION)
                  and d["total_dist_field"] is not None
                  and total_distance_raw > 0):
                rec = bytearray(raw[offset: offset + total_size])
                off = 1 + d["total_dist_field"]["data_offset"]
                struct.pack_into(d["endian"] + "I", rec, off, total_distance_raw)
                new_records += bytes(rec)
            else:
                new_records += bytes(raw[offset: offset + total_size])

            offset += total_size

    # --- Rebuild file with updated header and CRC ---
    new_rec_size = len(new_records)
    new_header   = bytearray(raw[:header_size])
    struct.pack_into("<I", new_header, 4, new_rec_size)
    if header_size >= 14:
        struct.pack_into("<H", new_header, 12, _fit_crc16(bytes(new_header[:12])))

    file_body = bytes(new_header) + bytes(new_records)
    output_path.write_bytes(file_body + struct.pack("<H", _fit_crc16(file_body)))

    log.info(
        "Merge complete: injected ICG data into %d/%d Garmin records (%.0f%%). Output: %s",
        injected,
        total_rec,
        100 * injected / total_rec if total_rec else 0,
        output_path,
    )
    if total_rec and injected < total_rec * 0.5:
        log.warning(
            "Less than 50%% of records were matched. Check that both files "
            "cover the same time window (timestamps may be offset by timezone)."
        )


# ---------------------------------------------------------------------------
# Helpers for encoding injected field bytes
# ---------------------------------------------------------------------------

def _patch_overwrite(
    rec: bytearray,
    overwrite_fields: list[dict],
    endian: str,
    icg: Optional[RecordSnapshot],
) -> None:
    """Patch ICG values into fields that already exist in the record bytes."""
    for f in overwrite_fields:
        off = 1 + f["data_offset"]   # +1 for the record header byte
        fid = f["id"]
        if fid == _FID_POWER:
            val = icg.power if icg and icg.power is not None else _INVALID_UINT16
            struct.pack_into(endian + "H", rec, off, val)
        elif fid == _FID_CADENCE:
            rec[off] = icg.cadence if icg and icg.cadence is not None else _INVALID_UINT8
        elif fid == _FID_DISTANCE:
            val = int(icg.distance * 100) if icg and icg.distance is not None else _INVALID_UINT32
            struct.pack_into(endian + "I", rec, off, val)


def _encode_extra(
    extra_fields: list[dict],
    endian: str,
    icg: Optional[RecordSnapshot],
) -> bytes:
    """Return the injected field bytes for one record."""
    out = bytearray()
    for ef in extra_fields:
        fid = ef["id"]
        if fid == _FID_POWER:
            val = (icg.power if icg and icg.power is not None else None)
            out += struct.pack(endian + "H", val if val is not None else _INVALID_UINT16)
        elif fid == _FID_CADENCE:
            val = (icg.cadence if icg and icg.cadence is not None else None)
            out += bytes([val if val is not None else _INVALID_UINT8])
        elif fid == _FID_DISTANCE:
            val = (int(icg.distance * 100) if icg and icg.distance is not None else None)
            out += struct.pack(endian + "I", val if val is not None else _INVALID_UINT32)
    return bytes(out)


def _invalid_extra(extra_fields: list[dict], endian: str) -> bytes:
    """Return all-invalid placeholder bytes (used for compressed-timestamp records)."""
    return _encode_extra(extra_fields, endian, None)
