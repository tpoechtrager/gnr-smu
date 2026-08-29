#!/usr/bin/env python3
"""Print Granite Ridge's active physical PM-core slots.

Every CCD has eight physical PM-table positions.  The low byte read here is a
*disabled-slot* bitmap, so it is inverted before display: an active-mask bit
of one identifies a position whose per-core PM values may be shown.  The
bounded probe walks global slot bases 0..504 (up to 512 logical positions).

Run: sudo python3 tools/show_active_core_slots.py

The SMN driver interprets exactly one 32-bit word as a read request.  This
tool never sends a second word and never writes an SMN register value.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smn_telemetry import (granite_ridge_disabled_slots_address,
                           read_smn_u32)  # noqa: E402


ZEN5_FAMILY = 0x1A
GRANITE_RIDGE_MODEL = 0x44
MAX_SLOTS = 512
SLOTS_PER_CCD = 8


def cpu_signature(cpuinfo="/proc/cpuinfo"):
    """Return (family, model), or (None, None) if Linux did not report it."""
    family = model = None
    try:
        with open(cpuinfo, encoding="utf-8") as stream:
            for line in stream:
                key, sep, value = line.partition(":")
                if not sep:
                    continue
                if key.strip() == "cpu family":
                    family = int(value.strip(), 0)
                elif key.strip() == "model":
                    model = int(value.strip(), 0)
                if family is not None and model is not None:
                    return family, model
    except (OSError, ValueError):
        pass
    return family, model


def main():
    argparse.ArgumentParser(
        description="Scan up to 512 Granite Ridge PM slots and show active slots"
    ).parse_args()

    family, model = cpu_signature()
    if (family, model) != (ZEN5_FAMILY, GRANITE_RIDGE_MODEL):
        raise SystemExit(
            "refusing unknown CPU: expected Granite Ridge family 0x1a, "
            f"model 0x44; got family={family!r}, model={model!r}")
    if os.geteuid() != 0:
        raise SystemExit("SMN read requires root; run: sudo python3 "
                         "tools/show_active_core_slots.py")

    active_slots = []
    reads = 0
    seen_addresses = set()
    for ccd in range(0, MAX_SLOTS, SLOTS_PER_CCD):
        address = granite_ridge_disabled_slots_address(ccd)
        if address in seen_addresses:
            continue
        seen_addresses.add(address)
        raw = read_smn_u32(address)
        if raw is None:
            continue
        reads += 1
        # A zero response is the usual result for an unmapped read on some
        # systems; do not turn it into eight false active slots.
        if raw == 0:
            continue
        active_mask = (~raw) & 0xFF
        active_slots.extend(
            ccd + slot for slot in range(SLOTS_PER_CCD)
            if active_mask & (1 << slot)
        )

    if not reads:
        raise SystemExit("no topology masks could be read; ensure ryzen_smu is loaded "
                         "and run the command with sudo")
    print("Granite Ridge active physical PM slots (scan limit: 512)")
    print("Only active slots are listed; disabled and unavailable slots are omitted.")
    if not active_slots:
        print("No active slots reported.")
        return
    for slot in active_slots:
        print(f"  slot {slot:3} (CCD {slot // 8}, local {slot % 8})")
    print(f"Total active slots: {len(active_slots)}")


if __name__ == "__main__":
    main()
