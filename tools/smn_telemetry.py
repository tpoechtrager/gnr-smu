#!/usr/bin/env python3
"""Read-only SMN telemetry helpers for profile-approved sensor registers."""

import struct


SMN_PATH = "/sys/kernel/ryzen_smu_drv/smn"


def read_smn_u32(address):
    """Read one 32-bit SMN register through ryzen_smu."""
    try:
        # One little-endian word is a read request; no write payload is sent.
        with open(SMN_PATH, "wb") as stream:
            stream.write(struct.pack("<I", address))
        with open(SMN_PATH, "rb") as stream:
            data = stream.read(4)
        return struct.unpack("<I", data)[0] if len(data) == 4 else None
    except OSError:
        return None


def read_profile_prochot_status(profile):
    """Return the three profile-approved thermal status bits or ``None``."""
    address = getattr(profile, "prochot_smn_address", None)
    if profile is None or address is None:
        return {"prochot_ext": None, "prochot_cpu": None, "htc": None}
    raw = read_smn_u32(address)
    if raw is None:
        return {"prochot_ext": None, "prochot_cpu": None, "htc": None}
    return {
        "prochot_ext": bool(raw & profile.prochot_ext_mask),
        "prochot_cpu": bool(raw & profile.prochot_cpu_mask),
        "htc": bool(raw & profile.htc_mask),
    }


def decode_iod_smn_temperature(raw, valid_bit, field_shift):
    """Decode one profile-approved IOD lane, or return ``None`` if invalid."""
    if not raw & (1 << valid_bit):
        return None
    field = (raw >> field_shift) & 0xFFF
    # Bit 11 is the hardware validity indicator. The unsigned 12-bit field
    # intentionally remains unrestricted here: with the -49 °C calibration
    # offset, legitimate readings may be below 0 °C.
    return field * 0.125 - 49.0


def read_profile_iod_lanes(profile):
    """Return one decoded value per profile-approved IOD lane.

    ``None`` denotes an unavailable or invalid lane. The positional list lets
    callers preserve the profile's lane numbering while hiding unavailable
    rows from the user interface.
    """
    addresses = getattr(profile, "iod_smn_temp_addresses", ()) if profile else ()
    valid_bit = getattr(profile, "iod_smn_temp_valid_bit", 0) if profile else 0
    field_shift = getattr(profile, "iod_smn_temp_field_shift", 0) if profile else 0
    if not addresses or not valid_bit:
        return []
    values = []
    for address in addresses:
        raw = read_smn_u32(address)
        if raw is None:
            values.append(None)
            continue
        values.append(decode_iod_smn_temperature(raw, valid_bit, field_shift))
    return values
