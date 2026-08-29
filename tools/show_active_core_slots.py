#!/usr/bin/env python3
"""Print Granite Ridge physical PM slots enabled by the factory topology maps.

Every CCD has eight physical PM-table positions.  The low byte read here is a
*factory-disabled-slot* bitmap, so it is inverted before display: a set bit
in the inverted mask identifies a slot not disabled by the factory map. CCD
factory presence is checked first. These maps do not report BIOS-disabled
hardware.

Run: sudo python3 tools/show_active_core_slots.py

The SMN driver interprets exactly one 32-bit word as a read request.  This
tool never sends a second word and never writes an SMN register value.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hwgate import get_hardware_profile  # noqa: E402
from smn_telemetry import read_profile_factory_enabled_core_slots  # noqa: E402


def main():
    argparse.ArgumentParser(
        description="Show Granite Ridge PM slots enabled by factory topology maps"
    ).parse_args()

    if os.geteuid() != 0:
        raise SystemExit("SMN read requires root; run: sudo python3 "
                         "tools/show_active_core_slots.py")

    profile, reason = get_hardware_profile()
    if profile is None:
        raise SystemExit(f"unsupported or unavailable profile: {reason}")
    factory_enabled_slots = read_profile_factory_enabled_core_slots(profile)
    if factory_enabled_slots is None:
        raise SystemExit("no factory core map could be read; ensure ryzen_smu is loaded "
                         "and run the command with sudo")
    print("Granite Ridge factory-enabled physical PM slots")
    print("Only slots not disabled by the factory maps are listed.")
    print("This is not a BIOS runtime core/CCD status query.")
    if not factory_enabled_slots:
        print("No factory-enabled slots reported.")
        return
    for slot in factory_enabled_slots:
        print(f"  slot {slot:3} (CCD {slot // 8}, local {slot % 8})")
    print(f"Total factory-enabled slots: {len(factory_enabled_slots)}")


if __name__ == "__main__":
    main()
