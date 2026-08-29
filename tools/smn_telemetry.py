#!/usr/bin/env python3
"""Read-only SMN telemetry helpers for profile-approved sensor registers."""

import struct


SMN_PATH = "/sys/kernel/ryzen_smu_drv/smn"
GNR_FACTORY_DISABLED_SLOTS_BASE = 0x304A03DC
GNR_FACTORY_CCD_PRESENT_FUSE = 0x5D3BC
GNR_FACTORY_CCD_DOWN_FUSE = 0x5D3C0


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


def granite_ridge_factory_disabled_slots_address(ccd_slot_base):
    """Return the read-only factory-disabled-slot address for one CCD base.

    ``ccd_slot_base`` is the global PM-slot base (0 for CCD0, 8 for CCD1).
    A CCD has eight physical PM-table positions. The register's low byte has
    one bit per position: set means factory-disabled, clear means not disabled
    by this factory map. It does not report a BIOS runtime configuration.
    This helper only calculates the address; it does not access hardware.
    """
    if not 0 <= ccd_slot_base < 512 or ccd_slot_base % 8:
        raise ValueError("CCD base must be an 8-slot boundary in range 0..504")
    # The register is selected by CCD ordinal, not by the global slot base.
    # CCD0 is 0x304A03DC; CCD1 is 0x324A03DC.
    return (GNR_FACTORY_DISABLED_SLOTS_BASE + ((ccd_slot_base // 8) << 25)) & 0xFFFFFFFF


def read_profile_factory_enabled_ccds(profile):
    """Return CCD ordinals enabled by the factory-level state fields.

    The PM format supplies the maximum CCD count; these two SMN words supply
    the per-package CCD state. ``ccdEnableMap`` is bits 22..23 of the first
    word. ``ccdDisableMap`` combines bits 30..31 of that word with bits 0..5
    of the second word shifted by two. A CCD is factory-enabled only when its
    enable bit is set and its disable bit is clear. The fields are not a
    verified BIOS runtime-online mask. ``None`` means the state could not be
    read; an empty tuple means no CCD in the profile is factory-enabled.
    """
    ccd_count = getattr(profile, "ccd_count", 0) if profile else 0
    if ccd_count not in (1, 2):
        return None
    present = read_smn_u32(GNR_FACTORY_CCD_PRESENT_FUSE)
    down = read_smn_u32(GNR_FACTORY_CCD_DOWN_FUSE)
    if present is None or down is None:
        return None
    ccd_enable_map = (present >> 22) & 0x3
    ccd_disable_map = ((present >> 30) & 0x3) | ((down & 0x3F) << 2)
    return tuple(
        ccd for ccd in range(ccd_count)
        if (ccd_enable_map & (1 << ccd))
        and not (ccd_disable_map & (1 << ccd))
    )


def read_profile_factory_enabled_core_slots(profile):
    """Return physical PM slots not disabled by the factory topology maps.

    The PM table is laid out by physical position, not by the number of cores
    sold in a SKU.  An 8-slot table therefore has positions 0..7 even on a
    six-core CPU; a 16-slot table has positions 0..15.  This function returns
    only positions in a factory-enabled CCD whose per-CCD factory-disable bit
    is clear, for example ``(0, 1, 3)``. It deliberately does not infer BIOS
    core or CCD state.

    Returns ``None`` when the mask cannot be read.  A generic decoder must
    treat that as unavailable topology, never as permission to substitute a
    dense range based on the operating system's core count.
    """
    slot_count = getattr(profile, "slot_count", 0) if profile else 0
    if slot_count not in (8, 16):
        return None
    factory_disabled_masks = read_profile_factory_disabled_core_masks(profile)
    if factory_disabled_masks is None:
        return None
    factory_enabled_slots = []
    for ccd, factory_disabled in factory_disabled_masks.items():
        ccd_base = ccd * 8
        factory_enabled_mask = (~factory_disabled) & 0xFF
        factory_enabled_slots.extend(
            ccd_base + slot for slot in range(8)
            if factory_enabled_mask & (1 << slot)
        )
    # An all-disabled mask cannot describe a usable profile and would make
    # later averages undefined.  Report it as unusable topology instead.
    return tuple(factory_enabled_slots) if factory_enabled_slots else None


def read_profile_factory_disabled_core_masks(profile):
    """Return ``{ccd: factory_disabled_low_byte}`` for factory-enabled CCDs.

    This is the same representation used by the established topology
    implementation: one eight-bit factory-disable map per factory-enabled CCD.
    A set bit is a factory-disabled physical core position; callers invert the byte
    to obtain PM slots not disabled by the factory map. Factory CCD presence
    is resolved first, so a factory-disabled CCD cannot make stale values in
    its slot mask look like factory-enabled cores. These are factory topology fields,
    not a verified indication of cores disabled through the BIOS.

    ``None`` means that the profile is unsupported or any required SMN read
    failed.  The raw high bits are deliberately discarded because only the
    low byte is defined as the per-core map for this format.
    """
    slot_count = getattr(profile, "slot_count", 0) if profile else 0
    if slot_count not in (8, 16):
        return None
    factory_enabled_ccds = read_profile_factory_enabled_ccds(profile)
    if factory_enabled_ccds is None:
        return None
    factory_disabled_masks = {}
    for ccd in factory_enabled_ccds:
        raw = read_smn_u32(granite_ridge_factory_disabled_slots_address(ccd * 8))
        if raw is None:
            return None
        factory_disabled_masks[ccd] = raw & 0xFF
    return factory_disabled_masks


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
