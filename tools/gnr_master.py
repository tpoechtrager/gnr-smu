#!/usr/bin/env python3
import sys
import os
import struct

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hwgate import (curve_optimizer_command, get_hardware_profile,
                    msg_id_blocked, smu_message_supported,
                    smu_writes_supported)

# Stock limits and MP1 message IDs live on the detected hardware profile. The
# never-send list also lives in hwgate.py so CLI, GUI and research tools cannot drift.


def apply_cmd(msg_id, arg0):
    ok, why = smu_writes_supported()
    if not ok:
        print(f"[BLOCKED] SMU writes disabled: {why}")
        return False
    profile, _ = get_hardware_profile()
    if not smu_message_supported(profile, msg_id):
        print(f"[BLOCKED] MSG 0x{msg_id:02x} is not in the {profile.name} allowlist")
        return False
    blocked, reason = msg_id_blocked(msg_id)
    if blocked:
        print(f"[BLOCKED] guardrail: {reason}")
        return False

    smu_args = "/sys/kernel/ryzen_smu_drv/smu_args"
    smu_cmd = "/sys/kernel/ryzen_smu_drv/mp1_smu_cmd"
    try:
        with open(smu_args, "wb") as f:
            f.write(struct.pack("<6I", arg0, 0, 0, 0, 0, 0))
        with open(smu_cmd, "wb") as f:
            f.write(struct.pack("<I", msg_id))
        
        with open(smu_cmd, "rb") as f:
            rsp = struct.unpack("<I", f.read(4))[0]

        rsp_name = {
            1: "OK",
            0xFD: "REJECTED",
            0xFE: "UNKNOWN_CMD",
            0xFF: "FAILED",
        }.get(rsp, f"0x{rsp:02X}")
        success = rsp == 1
        level = "OK" if success else "ERROR"
        print(f"[{level}] Sent MSG=0x{msg_id:02x} ARG={arg0} => RSP: {rsp_name}")
        return success
    except Exception as e:
        print(f"[ERROR] Driver write failed: {e}")
        return False

def ask_limit(name, unit, max_val):
    """Bounded numeric input. The GUI clamps these with spin-box ranges; the CLI took
    any float and sent it straight to the SMU, so a typo was a hardware command.
    Returns None if the value is out of range or unparseable."""
    raw = input(f"{name} ({unit}, 0-{max_val}): ")
    try:
        v = float(raw)
    except ValueError:
        print(f"[ERROR] not a number: {raw!r}")
        return None
    if not 0 <= v <= max_val:
        print(f"[ERROR] {name} must be between 0 and {max_val} {unit}, got {v}")
        return None
    return v


def ask_thermal_limit():
    """Read a whole-degree Tctl/HTC limit in the SMU-supported range."""
    raw = input("Thermal Limit (°C, 52-100): ")
    try:
        value = float(raw)
    except ValueError:
        print(f"[ERROR] not a number: {raw!r}")
        return None
    if value < 52 or value > 100 or not value.is_integer():
        print(f"[ERROR] Thermal Limit must be a whole value between 52 and 100 °C, got {value}")
        return None
    return value


def main():
    profile, _ = get_hardware_profile()
    cores = profile.cores if profile else 8
    writes_ok, writes_why = smu_writes_supported()
    if not writes_ok:
        print(f"[BLOCKED] This CLI only performs SMU writes: {writes_why}")
        print("Use export_telemetry.py --temps for read-only per-core temperatures.")
        return
    print("--- GNR Master Control ---")
    print("1. Set PPT Limit (Watts)")
    print("2. Set Custom TDC (Amps)")
    print("3. Set Custom EDC (Amps)")
    print("4. Set Thermal Limit (°C)")
    print("5. Apply -30 CO All Cores")
    print("6. Reset All Settings")
    print("7. Quit")
    
    choice = input("Option: ")
    
    if choice == '1':
        w = ask_limit("PPT", "Watts", 250)
        if w is not None:
            apply_cmd(profile.ppt_msg, int(w * 1000))
    elif choice == '2':
        a = ask_limit("TDC", "Amps", 200)
        if a is not None:
            apply_cmd(profile.tdc_msg, int(a * 1000))
    elif choice == '3':
        a = ask_limit("EDC", "Amps", 250)
        if a is not None:
            apply_cmd(profile.edc_msg, int(a * 1000))
    elif choice == '4':
        if profile.thermal_msg is None:
            print(f"[BLOCKED] Thermal Limit is not supported by the {profile.name} profile")
            return
        value = ask_thermal_limit()
        if value is not None:
            apply_cmd(profile.thermal_msg, int(value))
    elif choice == '5':
        applied = True
        for i in range(cores):
            msg_id, arg0 = curve_optimizer_command(profile, i, -30)
            if not apply_cmd(msg_id, arg0):
                applied = False
                break
        if applied:
            print("CO -30 applied.")
    elif choice == '6':
        applied = apply_cmd(profile.ppt_msg, profile.stock_ppt * 1000)
        if applied:
            applied = apply_cmd(profile.tdc_msg, profile.stock_tdc * 1000)
        if applied:
            applied = apply_cmd(profile.edc_msg, profile.stock_edc * 1000)
        if applied:
            for i in range(cores):
                msg_id, arg0 = curve_optimizer_command(profile, i, 0)
                if not apply_cmd(msg_id, arg0):
                    applied = False
                    break
        if applied:
            print("Reset successful.")
    
if __name__ == "__main__":
    main()
