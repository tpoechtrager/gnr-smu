#!/usr/bin/env python3
"""Read-only SMN telemetry helpers for profile-approved sensor registers."""

import struct


SMN_PATH = "/sys/kernel/ryzen_smu_drv/smn"
GNR_DISABLED_SLOTS_BASE_LOW = 0x304A03DC
GNR_DISABLED_SLOTS_BASE_HIGH = 0x3A4A03DC


def read_smn_u32(address):
    """Read one 32-bit SMN register through ``ryzen_smu``.

    The kernel interface treats exactly one little-endian word as the address
    of a read transaction.  A register value is never sent by this helper.
    ``None`` means the driver or the requested read was unavailable.
    """
    try:
        # One little-endian word is a read request; no write payload is sent.
        with open(SMN_PATH, "wb") as stream:
            stream.write(struct.pack("<I", address))
        with open(SMN_PATH, "rb") as stream:
            data = stream.read(4)
        return struct.unpack("<I", data)[0] if len(data) == 4 else None
    except OSError:
        return None


def granite_ridge_disabled_slots_address(ccd):
    """Return the read-only disabled-slot bitmap address for one CCD base.

    ``ccd`` is the global PM-slot base (0 for CCD0, 8 for CCD1).  A CCD has
    eight physical PM-table positions.  The register's low byte has one bit
    per position: set means disabled, clear means usable.  This helper only
    calculates the address; it does not access hardware.
    """
    if not 0 <= ccd < 512 or ccd % 8:
        raise ValueError("CCD base must be an 8-slot boundary in range 0..504")
    base = GNR_DISABLED_SLOTS_BASE_LOW if ccd < 8 else GNR_DISABLED_SLOTS_BASE_HIGH
    # The driver accepts a 32-bit SMN address.  Keep speculative high-index
    # probes in that address space; the caller deduplicates any aliases caused
    # by the bounded arithmetic wraparound.
    return (base + (ccd << 25)) & 0xFFFFFFFF


def read_profile_active_core_slots(profile):
    """Discover the usable physical PM slots for a Granite Ridge profile.

    The PM table is laid out by physical position, not by the number of cores
    sold in a SKU.  An 8-slot table therefore has positions 0..7 even on a
    six-core CPU; a 16-slot table has positions 0..15.  This function returns
    only positions enabled by the hardware bitmap, for example ``(0, 1, 3)``.

    Returns ``None`` when the mask cannot be read.  A generic decoder must
    treat that as unavailable topology, never as permission to substitute a
    dense range based on the operating system's core count.
    """
    slot_count = getattr(profile, "slot_count", 0) if profile else 0
    if slot_count not in (8, 16):
        return None
    active = []
    # The topology register uses the global PM-slot index.  CCD0 covers
    # slots 0..7 and CCD1 covers slots 8..15; using the ordinal (0, 1) for
    # the second read addresses the wrong SMN region and can make disabled
    # slots look active.
    for ccd_base in range(0, slot_count, 8):
        raw = read_smn_u32(granite_ridge_disabled_slots_address(ccd_base))
        if raw is None:
            return None
        mask = (~raw) & 0xFF
        active.extend(ccd_base + slot for slot in range(8)
                      if mask & (1 << slot))
    # An all-disabled mask cannot describe a usable profile and would make
    # later averages undefined.  Report it as unusable topology instead.
    return tuple(active) if active else None


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
