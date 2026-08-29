#!/usr/bin/env python3
"""Probe direct CCD sensors, factory topology fields, and raw PM lanes.

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
    GNR_FACTORY_CCD_DOWN_FUSE,
    GNR_FACTORY_CCD_PRESENT_FUSE,
    granite_ridge_factory_disabled_slots_address,
    read_profile_factory_enabled_ccds,
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
    ccd_present = read_smn_u32(GNR_FACTORY_CCD_PRESENT_FUSE)
    ccd_down = read_smn_u32(GNR_FACTORY_CCD_DOWN_FUSE)
    factory_enabled_ccds = read_profile_factory_enabled_ccds(profile)
    print("CCD state:")
    if ccd_present is None or ccd_down is None:
        print("  unavailable (CCD state fields could not be read)")
    else:
        enable_map = (ccd_present >> 22) & 0x3
        disable_map = ((ccd_present >> 30) & 0x3) | ((ccd_down & 0x3F) << 2)
        print(f"  present: SMN 0x{GNR_FACTORY_CCD_PRESENT_FUSE:05X} raw=0x{ccd_present:08X}")
        print(f"  down:    SMN 0x{GNR_FACTORY_CCD_DOWN_FUSE:05X} raw=0x{ccd_down:08X}")
        print(f"  factory enable map: 0b{enable_map:02b}; factory disable map: 0b{disable_map:08b}")
    if factory_enabled_ccds is None:
        print("  factory-enabled CCDs: unavailable")
    else:
        print(f"  factory-enabled CCDs: {', '.join(str(ccd + 1) for ccd in factory_enabled_ccds) or 'none'}")
    print("  note: factory maps do not identify BIOS-disabled CCDs or cores")
    print("\nDirect CCD temperature registers:")
    for ccd, address in enumerate(profile.ccd_smn_temp_addresses):
        raw = read_smn_u32(address)
        valid, temperature = decode_direct_ccd_temperature(raw)
        raw_text = "unavailable" if raw is None else f"0x{raw:08X}"
        value_text = f"{temperature:.3f} °C" if valid else "-- (valid bit clear)"
        print(f"  CCD{ccd + 1}: SMN 0x{address:05X} raw={raw_text}; {value_text}")

    print("\nFactory topology masks (low byte: factory-disabled bits; inverted: usable bits):")
    for ccd_base in range(0, profile.slot_count, 8):
        address = granite_ridge_factory_disabled_slots_address(ccd_base)
        raw = read_smn_u32(address)
        if raw is None:
            print(f"  slots {ccd_base:2}..{ccd_base + 7}: SMN 0x{address:08X} unavailable")
            continue
        factory_disabled = raw & 0xFF
        factory_enabled = (~factory_disabled) & 0xFF
        print(
            f"  slots {ccd_base:2}..{ccd_base + 7}: SMN 0x{address:08X} "
            f"raw=0x{raw:08X}, factory_disabled=0b{factory_disabled:08b}, "
            f"factory_enabled=0b{factory_enabled:08b}"
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
