#!/usr/bin/env python3
"""Probe direct CCD sensors and raw PM lanes, including disabled CCDs.

This diagnostic is read-only at the hardware level.  It requires root because
the SMN interface accepts a register address as the read request.  A direct
CCD temperature is valid only when bit 11 is set in the returned register;
the PM-table values are printed separately and must not be confused with that
direct sensor.

Run:
    sudo python3 tools/probe_disabled_ccd.py
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hwgate import get_hardware_profile  # noqa: E402
from smn_telemetry import (  # noqa: E402
    granite_ridge_disabled_slots_address,
    read_smn_u32,
)


SMN_CCD_TEMP_VALID_BIT = 11
SMN_CCD_TEMP_VALUE_MASK = 0x7FF
PM_CORE_TEMP_LABEL = "core temperature"


def decode_direct_ccd_temperature(raw):
    """Return ``(valid, temperature_c)`` for one direct CCD register."""
    if raw is None or not raw & (1 << SMN_CCD_TEMP_VALID_BIT):
        return False, None
    return True, (raw & SMN_CCD_TEMP_VALUE_MASK) * 0.125 - 49.0


def read_pm_table(profile):
    """Read the profile-sized PM table and return its float tuple, if possible."""
    try:
        with open("/sys/kernel/ryzen_smu_drv/pm_table", "rb") as stream:
            data = stream.read(profile.table_size)
        if len(data) != profile.table_size:
            return None
        return struct.unpack(f"<{profile.float_count}f", data)
    except (OSError, struct.error):
        return None


def main():
    if os.geteuid() != 0:
        raise SystemExit("SMN probing requires root; run with sudo")

    profile, reason = get_hardware_profile()
    if profile is None:
        raise SystemExit(f"unsupported or unavailable profile: {reason}")

    print(f"CPU/profile: {profile.name}")
    print(f"PM table: version 0x{profile.pm_version:06X}, {profile.table_size} bytes")
    print("\nDirect CCD temperature registers:")
    for ccd, address in enumerate(profile.ccd_smn_temp_addresses):
        raw = read_smn_u32(address)
        valid, temperature = decode_direct_ccd_temperature(raw)
        raw_text = "unavailable" if raw is None else f"0x{raw:08X}"
        value_text = f"{temperature:.3f} °C" if valid else "-- (valid bit clear)"
        print(f"  CCD{ccd + 1}: SMN 0x{address:05X} raw={raw_text}; {value_text}")

    print("\nTopology masks (low byte: disabled bits; inverted: active bits):")
    for ccd_base in range(0, profile.slot_count, 8):
        address = granite_ridge_disabled_slots_address(ccd_base)
        raw = read_smn_u32(address)
        if raw is None:
            print(f"  slots {ccd_base:2}..{ccd_base + 7}: SMN 0x{address:08X} unavailable")
            continue
        disabled = raw & 0xFF
        active = (~disabled) & 0xFF
        print(
            f"  slots {ccd_base:2}..{ccd_base + 7}: SMN 0x{address:08X} "
            f"raw=0x{raw:08X}, disabled=0b{disabled:08b}, active=0b{active:08b}"
        )

    table = read_pm_table(profile)
    print("\nRaw PM lanes:")
    if table is None:
        print("  PM table unavailable")
        return
    for slot in range(profile.slot_count):
        print(f"  slot {slot:2}: d[{profile.core_temp + slot}] {PM_CORE_TEMP_LABEL} = "
              f"{table[profile.core_temp + slot]:.6f}")
    if profile.ccd_l3_temperature is not None:
        for ccd in range(profile.ccd_count):
            print(f"  CCD{ccd + 1} L3: d[{profile.ccd_l3_temperature + ccd}] = "
                  f"{table[profile.ccd_l3_temperature + ccd]:.6f}")


if __name__ == "__main__":
    main()
