#!/usr/bin/env python3
"""Read-only Granite Ridge IOD SMN probe.

Run with sudo on an exact supported profile.  This does not write an SMN
value: each transaction supplies exactly one little-endian address word to
the ryzen_smu read interface.  It prints raw values and both temperature
encodings for the IOD blocks selected by the documented accessor.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hwgate import get_hardware_profile
from smn_telemetry import decode_iod_smn_temperature, read_smn_u32


LANES = (1, 2, 4, 5)
BLOCKS = (0x59824, 0x59888, 0x598A4, 0x59914, 0x59834,
          0x598B4, 0x599A4, 0x599C0, 0x59A30, 0x598C4)


def decode_bit10(raw):
    if not raw & (1 << 10):
        return None
    return ((raw >> 11) & 0xFFF) * 0.125 - 49.0


def decode_bit11(raw):
    if not raw & (1 << 11):
        return None
    return ((raw >> 12) & 0xFFF) * 0.125 - 49.0


def main():
    profile, reason = get_hardware_profile()
    if profile is None:
        sys.exit(f"refusing probe: {reason}")
    print(f"profile: {reason}")
    print("profile decoder: "
          f"bit {profile.iod_smn_temp_valid_bit}, "
          f"field bits {profile.iod_smn_temp_field_shift}.."
          f"{profile.iod_smn_temp_field_shift + 11}")
    for base in BLOCKS:
        values = []
        for lane in LANES:
            address = base + 4 * lane
            raw = read_smn_u32(address)
            if raw is None:
                sys.exit("SMN read unavailable; run this script as root")
            selected = decode_iod_smn_temperature(
                raw, profile.iod_smn_temp_valid_bit, profile.iod_smn_temp_field_shift
            )
            values.append(
                f"lane {lane:2d} @ 0x{address:05x}: 0x{raw:08x} "
                f"profile={selected!s:>7} bit10={decode_bit10(raw)!s:>7} "
                f"bit11={decode_bit11(raw)!s:>7}"
            )
        print(f"base 0x{base:05x}")
        print("\n".join(f"  {value}" for value in values))


if __name__ == "__main__":
    main()
