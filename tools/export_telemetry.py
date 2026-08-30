#!/usr/bin/env python3
"""
GNR-SMU Telemetry Exporter — Granite Ridge (Zen 5)
Usage:
  python3 export_telemetry.py              # 5 JSON snapshots -> gnr_telemetry_dump.json
  python3 export_telemetry.py --csv        # named CSV snapshot -> gnr_telemetry.csv
  python3 export_telemetry.py --live N     # CSV live logging every N seconds (Ctrl+C to stop)
  python3 export_telemetry.py --temps      # print one per-core temperature snapshot
"""
import struct
import json
import time
import csv
import sys
import argparse
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hwgate import get_hardware_profile  # noqa: E402
from smn_telemetry import (read_profile_factory_enabled_core_slots, read_profile_iod_lanes,
                           read_profile_prochot_status)  # noqa: E402

PM_TABLE_PATH = "/sys/kernel/ryzen_smu_drv/pm_table"
VERSION_PATH  = "/sys/kernel/ryzen_smu_drv/pm_table_version"

JSON_OUTPUT   = "gnr_telemetry_dump.json"
CSV_OUTPUT    = "gnr_telemetry.csv"

# Key named fields: (column_name, float_index, unit).
COMMON_FIELDS = [
    ("timestamp",        None,  "s"),
    # Zone 0x000 is the Zen (LIMIT, VALUE) pair layout — corrected 2026-07-30.
    # d[8] is TDC (not EDC) and d[10] is the thermal limit in °C (not TDC in A).
    ("ppt_limit",        2,     "W"),
    ("ppt_value",        3,     "W"),    # total package power, incl. SoC/uncore
    ("tdc_limit",        8,     "A"),
    ("tdc_value",        9,     "A"),
    ("thm_limit",        10,    "C"),
    ("tctl",             11,    "C"),    # direct °C, matches k10temp Tctl
    ("edc_limit",        63,    "A"),
    ("vcore_peak",       None,  "V"),   # computed from the profile's core-voltage block
    ("vcore_avg",        None,  "V"),
    ("fclk",             71,    "MHz"),
    ("uclk",             75,    "MHz"),
    ("mclk",             79,    "MHz"),
]

# These iGPU values are read only from shared CPU PM-table positions.
IGPU_PM_FIELDS = [
    ("igpu_power", 107, "W"),
    ("igpu_clock", 108, "MHz"),
]


def global_fields(profile):
    """Return only fields whose meaning is established for this table version."""
    fields = list(COMMON_FIELDS) + list(IGPU_PM_FIELDS)
    if profile.pm_version == 0x620205:
        fields += [
            # Idle/load PM-table evidence: d[105] tracks a live GFX/iGPU
            # voltage-like value and d[106] tracks direct degrees Celsius.
            # Keep both mappings version-gated until another table layout is
            # independently verified.
            ("igpu_voltage", 105, "V"),
            ("igpu_temperature", 106, "C"),
            ("igpu_busy_pm_candidate", 110, "%"),
            ("igpu_idle_pm_candidate", 187, "%"),
            ("fit_metric", 16, "metric"),
            ("vid_limit", 18, "V"),
            ("vid_live", 19, "V"),
            ("vddcr_cpu_power", 20, "W"),
            ("vddcr_soc_power", 21, "W"),
            ("vddio_mem_power", 22, "W"),
            ("vdd18_power", 23, "W"),
            ("socket_power", 26, "W"),
            ("vdd_misc", 58, "V"),
            ("vddcr_soc", 83, "V"),
            ("cldo_vddg_iod", 259, "V"),
            ("cldo_vddg_ccd", 261, "V"),
            ("cldo_vddp", 269, "V"),
        ]
    else:
        fields += [
            ("hotspot_temp", 270, "C"),
            ("pkg_power", 20, "W"),
            ("soc_power", 21, "W"),
            ("soc_telemetry", 87, "metric"),
            ("soc_telemetry_metric", 95, "unit"),
            ("slow_temp_0", 298, "C"),
            ("slow_temp_1", 299, "C"),
            ("pkg_energy", 212, "J"),
        ]
    return fields


def factory_enabled_slots(profile):
    """Return PM positions not disabled by the factory topology maps.

    Generic profiles have known binary layout but not a SKU-specific topology.
    They must read the factory topology maps so factory-disabled lanes cannot
    leak into the output as fake core sensors. Exact profiles retain their
    established dense fallback when direct topology access is unavailable.
    """
    slots = read_profile_factory_enabled_core_slots(profile)
    if slots is not None:
        return slots
    if profile.requires_topology_mask:
        sys.exit("cannot read Granite Ridge factory topology maps; run with sudo so "
                 "factory-disabled PM slots are never exported as cores")
    return tuple(range(profile.slot_count or profile.cores))


def named_fields(profile, slots=None):
    fields = global_fields(profile)
    slots = factory_enabled_slots(profile) if slots is None else slots
    if profile.ccd_l3_temperature is not None:
        for ccd in range(profile.ccd_count):
            fields.append(
                (f"ccd{ccd}_l3_cache", profile.ccd_l3_temperature + ccd, "C")
            )
    if profile.prochot_smn_address is not None:
        fields += [
            ("prochot_cpu", None, "Yes/No"),
            ("prochot_ext", None, "Yes/No"),
            ("htc", None, "Yes/No"),
        ]
    if profile.iod_smn_temp_addresses:
        fields += [(f"iod_lane_{lane}", None, "C")
                   for lane in range(len(profile.iod_smn_temp_addresses))]
    for core in slots:
        fields.append((f"c{core}_power", profile.core_power + core, "W"))
        fields.append((f"c{core}_voltage", profile.core_voltage + core, "V"))
        fields.append((f"c{core}_temp", profile.core_temp + core, "C"))
        if profile.core_frequency is not None:
            fields.append(
                (f"c{core}_frequency", profile.core_frequency + core, "GHz")
            )
        if profile.core_fit is not None:
            fields.append((f"c{core}_fit", profile.core_fit + core, "metric"))
        if profile.core_activity is not None:
            activity_name = (f"c{core}_activity_metric"
                             if profile.core_c0 is not None
                             else f"c{core}_light_cstate_metric")
            fields.append(
                (activity_name, profile.core_activity + core, "metric")
            )
        if profile.core_c0 is not None:
            fields.append((f"c{core}_c0_residency", profile.core_c0 + core, "%"))
        if profile.core_cc1 is not None:
            fields.append((f"c{core}_cc1_residency", profile.core_cc1 + core, "%"))
        fields.append((f"c{core}_cc6_residency", profile.core_cc6 + core, "%"))
        boost_name = (f"c{core}_boost_limit" if profile.boost_limit_confident
                      else f"c{core}_boost_limit_candidate")
        fields.append((boost_name, profile.core_boost_limit + core, "GHz"))
    return fields


def get_pm_version():
    try:
        with open(VERSION_PATH, "rb") as f:
            return struct.unpack("<I", f.read(4))[0]
    except Exception:
        return 0


def require_supported_hardware():
    """Exit rather than export named fields read from unvalidated offsets.

    This used to print a warning and carry on, which is the worst option: the CSV
    still has a `Package_Power_W` column, it still holds a plausible number, and
    nothing downstream can tell it came from the wrong offset.
    """
    profile, why = get_hardware_profile()
    if profile is None:
        sys.exit(f"refusing to export: {why}\n"
                 f"see tools/hwgate.py — the field names would be wrong, not missing.")
    print(f"hardware check: {why}")
    return profile


def get_floats(profile):
    with open(PM_TABLE_PATH, "rb") as f:
        data = f.read(profile.table_size)
    if len(data) != profile.table_size:
        raise ValueError(f"Unexpected table size: {len(data)} bytes")
    return list(struct.unpack(f"<{profile.float_count}f", data))


def floats_to_row(d, ts, profile, fields=None, slots=None):
    row = {}
    slots = factory_enabled_slots(profile) if slots is None else slots
    fields = fields or named_fields(profile, slots)
    vcores = [d[profile.core_voltage + slot] for slot in slots]
    thermal = read_profile_prochot_status(profile)
    iod_lanes = read_profile_iod_lanes(profile)
    for name, idx, _ in fields:
        if name == "timestamp":
            row[name] = f"{ts:.3f}"
        elif name == "vcore_peak":
            row[name] = f"{max(vcores):.4f}"
        elif name == "vcore_avg":
            row[name] = f"{sum(vcores)/len(vcores):.4f}"
        elif name in thermal:
            value = thermal[name]
            row[name] = "Unavailable" if value is None else ("Yes" if value else "No")
        elif name.startswith("iod_lane_"):
            lane = int(name.rsplit("_", 1)[1])
            value = iod_lanes[lane] if lane < len(iod_lanes) else None
            row[name] = "Unavailable" if value is None else f"{value:.4f}"
        else:
            row[name] = f"{d[idx]:.4f}"
    return row


def csv_output_mode(fieldnames, live_interval):
    """Return ``(mode, write_header)`` without appending to an incompatible CSV."""
    append = (live_interval is not None and os.path.exists(CSV_OUTPUT)
              and os.path.getsize(CSV_OUTPUT) > 0)
    if not append:
        return "w", True

    with open(CSV_OUTPUT, newline="") as f:
        existing_header = next(csv.reader(f), [])
    if existing_header != fieldnames:
        sys.exit(
            f"refusing to append to {CSV_OUTPUT}: its columns do not match the "
            "current hardware profile; move or remove the existing file first"
        )
    return "a", False


def cmd_json():
    profile = require_supported_hardware()
    ver = get_pm_version()
    snapshots = []
    print("Capturing 5 snapshots (10 seconds)...")
    for i in range(5):
        print(f"  Snapshot {i+1}/5...")
        snapshots.append(get_floats(profile))
        if i < 4:
            time.sleep(2)
    export_data = {
        "metadata": {
            "version": hex(ver),
            "table_size": profile.table_size,
            "float_count": profile.float_count,
            "notes": "Generated by GNR-SMU export tool",
            "processor": f"Granite Ridge (Zen 5) — {profile.name}",
        },
        "snapshots": snapshots,
    }
    with open(JSON_OUTPUT, "w") as f:
        json.dump(export_data, f, indent=2)
    print(f"\n✅ Exported {len(snapshots)} snapshots -> {JSON_OUTPUT}")


def cmd_csv(live_interval=None):
    profile = require_supported_hardware()
    slots = factory_enabled_slots(profile)
    fields = named_fields(profile, slots)
    fieldnames = [name for name, _, _ in fields]
    mode, write_header = csv_output_mode(fieldnames, live_interval)
    with open(CSV_OUTPUT, mode, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()

        if live_interval is None:
            d = get_floats(profile)
            writer.writerow(floats_to_row(d, time.time(), profile, fields, slots))
            print(f"✅ Snapshot -> {CSV_OUTPUT}")
        else:
            print(f"Live logging every {live_interval}s -> {CSV_OUTPUT}  (Ctrl+C to stop)")
            n = 0
            try:
                while True:
                    d = get_floats(profile)
                    writer.writerow(floats_to_row(d, time.time(), profile, fields, slots))
                    f.flush()
                    n += 1
                    pkg = d[3]
                    max_temp = max(d[profile.core_temp + slot] for slot in slots)
                    print(f"\r  [{n}] Pkg: {pkg:.1f}W  MaxTemp: {max_temp:.1f}°C", end="", flush=True)
                    time.sleep(live_interval)
            except KeyboardInterrupt:
                print(f"\n✅ Stopped after {n} samples.")


def cmd_temps():
    profile = require_supported_hardware()
    d = get_floats(profile)
    print(f"Per-core temperatures — {profile.name}")
    for slot in factory_enabled_slots(profile):
        print(f"Core slot {slot:2}: {d[profile.core_temp + slot]:5.1f} °C")


def main():
    parser = argparse.ArgumentParser(description="GNR-SMU Telemetry Exporter")
    parser.add_argument("--csv", action="store_true", help="Export named CSV snapshot")
    parser.add_argument("--live", type=float, metavar="N", help="Live CSV logging every N seconds")
    parser.add_argument("--temps", action="store_true",
                        help="Print one per-core temperature snapshot")
    args = parser.parse_args()

    if args.temps:
        cmd_temps()
    elif args.live is not None:
        cmd_csv(live_interval=args.live)
    elif args.csv:
        cmd_csv()
    else:
        cmd_json()


if __name__ == "__main__":
    main()
