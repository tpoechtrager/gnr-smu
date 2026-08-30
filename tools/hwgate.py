#!/usr/bin/env python3
"""Hardware profiles and safety gates for Granite Ridge PM-table tools.

Telemetry layouts are keyed by PM-table version, byte size and physical core count.
SMU writes have a separate, stricter gate: validating read-only telemetry on a CPU
does not establish that mailbox commands or Curve Optimizer IDs are safe on it.
"""

from dataclasses import dataclass, replace
import os
import struct
from typing import Optional

VERSION_PATH = "/sys/kernel/ryzen_smu_drv/pm_table_version"
SIZE_PATH = "/sys/kernel/ryzen_smu_drv/pm_table_size"
GRANITE_RIDGE_FAMILY = 0x1A
GRANITE_RIDGE_MODEL = 0x44


@dataclass(frozen=True)
class HardwareProfile:
    name: str
    cpu_model: str
    pm_version: int
    table_size: int
    cores: int
    core_power: int
    core_voltage: int
    core_temp: int
    core_frequency: Optional[int]
    core_fit: Optional[int]
    core_activity: Optional[int]
    core_light_cstate: Optional[int]
    core_c0: Optional[int]
    core_cc1: Optional[int]
    core_cc6: int
    core_boost_limit: int
    boost_limit_confident: bool
    # CCD-adjacent research fields. The L3 pair is kept separate below because
    # its identity is validated; neighbouring pairs remain named for what was
    # actually measured (shared/non-selective telemetry).
    # 2026-08-25: d[589]/d[590] and d[591]/d[592] were checked for CCD
    # selectivity the same way d[595]/d[596] were (busy-loop load pinned to
    # CCD0 only, then CCD1 only). Unlike d[595]/d[596], both lanes of each
    # pair rose almost identically regardless of which CCD was loaded
    # (+3.68/+3.14 for CCD0 load vs +3.84/+3.69 for CCD1 load) — they are
    # NOT per-CCD values and must not be presented as "CCDx power" in the
    # GUI. Kept here only as raw research fields.
    ccd_power_candidate: Optional[int]
    ccd_vddm_candidate: Optional[int]
    ccd_l3_temperature: Optional[int]
    ccd_count: int
    ppt_msg: int
    tdc_msg: int
    edc_msg: int
    stock_ppt: int
    stock_tdc: int
    stock_edc: int
    co_mode: str
    co_msg: int = 0
    allow_smu_writes: bool = False
    # Direct Tctl/Tdie register. It is separate from the PM table but shared
    # by the family/model-gated Granite Ridge format decoders.
    tctl_smn_address: Optional[int] = None
    # Direct CCD temperature registers. These read-only SMN locations are
    # separate from the PM table and explicit in every format decoder.
    ccd_smn_temp_addresses: tuple = ()
    # Read-only I/O-die lanes with a Granite-Ridge-established validity mask
    # and temperature encoding.
    iod_smn_temp_addresses: tuple = ()
    iod_smn_temp_valid_bit: int = 0
    iod_smn_temp_field_shift: int = 0
    iod_smn_lane_numbers: tuple = ()
    ccd_shared_temperature: Optional[int] = None
    edc_value: Optional[int] = None
    # Read-only thermal throttle status register and masks, shared by the
    # Granite Ridge family/model-gated format decoders.
    prochot_smn_address: Optional[int] = None
    prochot_ext_mask: int = 0
    prochot_cpu_mask: int = 0
    htc_mask: int = 0
    # MP1 command for the configurable Tctl/HTC temperature ceiling.  This is
    # intentionally profile-specific: command IDs are not portable across
    # SMU generations.
    thermal_msg: Optional[int] = None
    # Geometric PM lanes. This is deliberately separate from ``cores``:
    # a 6-/12-core part still has an 8-/16-slot PM format.
    slot_count: int = 0
    # Unlisted models require this read to identify populated PM lanes. Exact
    # write-approved models retain their established dense fallback.
    requires_topology_mask: bool = False
    # Profile-specific iGPU lanes. The key is a GUI/export identity and the
    # value is its PM-table float index; absent keys are not displayed.
    igpu_fields: tuple = ()
    # Some formats expose only a package-level FIT-related metric, rather than
    # the per-core lane used by the 8-slot format.
    global_fit: Optional[int] = None

    @property
    def float_count(self):
        return self.table_size // 4


@dataclass(frozen=True)
class WritePolicy:
    """One exact CPU/model authorization for mailbox writes.

    The policy deliberately contains no telemetry offsets. Those belong to a
    PM format decoder and are selected before this policy is considered.
    """
    name: str
    cpu_model: str
    pm_version: int
    table_size: int
    cores: int
    ppt_msg: int
    tdc_msg: int
    edc_msg: int
    stock_ppt: int
    stock_tdc: int
    stock_edc: int
    co_mode: str
    co_msg: int = 0
    thermal_msg: Optional[int] = None


# PM-table decoders are selected solely by their runtime header. They contain
# telemetry offsets and shared Granite Ridge read-only SMN paths, never a SKU
# write authorization.
FORMAT_PROFILES = {
    (0x620105, 1828): HardwareProfile(
        "Granite Ridge 8-slot PM format (read-only)", "", 0x620105, 1828, 0,
        core_power=333, core_voltage=309, core_temp=317, core_frequency=325,
        core_fit=None, core_activity=None, core_light_cstate=None,
        core_c0=341, core_cc1=349,
        core_cc6=357, core_boost_limit=373, boost_limit_confident=True,
        ccd_power_candidate=None, ccd_vddm_candidate=None,
        # Per-CCD L3 cache temperature for the 0x620105 layout.  This is the
        # profile-specific d[448 + CCD] lane; the 9800X3D has one CCD.
        ccd_l3_temperature=448, ccd_count=1,
        ppt_msg=0, tdc_msg=0, edc_msg=0,
        stock_ppt=0, stock_tdc=0, stock_edc=0,
        co_mode="read_only",
        tctl_smn_address=0x59800,
        # Linux k10temp maps Zen 5 Ryzen Desktop Tccd1 to 0x59800 + 0x308.
        ccd_smn_temp_addresses=(0x59B08,),
        # Granite Ridge IOD sensor block. The same read-only lane encoding is
        # used by the single-CCD 9800X3D profile.
        iod_smn_temp_addresses=(0x59828, 0x5982C, 0x59834, 0x59838),
        iod_smn_temp_valid_bit=11, iod_smn_temp_field_shift=12,
        iod_smn_lane_numbers=(1, 2, 4, 5),
        # Live EDC-current candidate immediately following EDC_LIMIT d[63].
        edc_value=64,
        prochot_smn_address=0x59804,
        prochot_ext_mask=0x04, prochot_cpu_mask=0x08, htc_mask=0x10,
        slot_count=8,
        requires_topology_mask=True,
        igpu_fields=(
            ("igpu_power", 107), ("igpu_clock", 108),
            ("igpu_activity", 109), ("igpu_current", 110),
            ("igpu_busy", 186), ("igpu_idle", 187),
        ),
    ),
    (0x620205, 2452): HardwareProfile(
        "Granite Ridge 16-slot PM format (read-only)", "", 0x620205, 2452, 0,
        core_power=301, core_voltage=317, core_temp=333, core_frequency=349,
        # Ryzen Master multiplies d[349+i] by 1000 before exposing its
        # per-core clock array. Live comparison on this 9950X3D found
        # d[349] = 5.472 GHz while Linux reported 5456 MHz for core 0.
        # Do not retain the previous FIT label for this frequency lane.
        core_fit=None, core_activity=365, core_light_cstate=None,
        core_c0=381, core_cc1=397,
        core_cc6=413, core_boost_limit=445, boost_limit_confident=False,
        # The neighbouring CCD fields remain explicitly separate from the
        # validated L3 pair until their identities are established.
        #
        # 2026-08-25, research/recheck_l3.py: loading CCD0 only raises d[595]
        # (+13.8 K over baseline) far more than d[596] (+6.5 K), and loading
        # CCD1 only reverses that — d[595]/d[596] are CCD-selective. d[611]/
        # d[612] move together almost identically regardless of which CCD is
        # loaded (shared/non-selective).
        #
        # 2026-08-25, research/l3_specificity.py: an ALU-only load and an
        # L3-cache-thrash load pinned to the same CCD were compared. Per K of
        # CCD-avg core-temp rise, the cache-thrash load moved every one of
        # these lanes 3-6x more than the ALU load did (e.g. d[595] rose x1.54
        # of the core-temp rise under cache-thrash vs only x0.43 under ALU
        # load) — suggestive of L3 coupling, but it was a single uncontrolled
        # run confounded by very different absolute core temperatures between
        # the two loads (ALU hit ~69 °C avg, cache-thrash only ~41 °C), and
        # cache-thrash also broke the earlier CCD-selectivity (d[596] rose
        # almost as much as d[595] despite only CCD0 being loaded). Not proof.
        #
        # 2026-08-25, research/l3_specificity_controlled.py: repeated the test
        # with the confound removed by throttling the ALU load (--cpu-load
        # duty cycling) until its CCD0-avg core-temp rise matched the
        # cache-thrash run's rise to within 0.44 K. At *matched* core-temp
        # rise, d[595]/d[596] still rose an extra +4.2 K / +7.9 K under
        # cache-thrash vs ALU — that excess cannot be explained by core
        # heating, and is real evidence of L3-traffic coupling for this pair.
        # d[611]/d[612] did the *opposite* (-2.7 K vs ALU at matched core
        # temp), ruling out an L3-cache identity for that pair; it tracks
        # something else (fabric/package-level heat, not cache traffic).
        # Single run so far; kept named for what is confirmed either way.
        ccd_shared_temperature=611,
        ccd_power_candidate=589, ccd_vddm_candidate=591,
        ccd_l3_temperature=595, ccd_count=2,
        ppt_msg=0, tdc_msg=0, edc_msg=0,
        stock_ppt=0, stock_tdc=0, stock_edc=0,
        co_mode="read_only",
        tctl_smn_address=0x59800,
        # Same Zen 5 Desktop register block: Tccd1, Tccd2.
        ccd_smn_temp_addresses=(0x59B08, 0x59B0C),
        # Granite Ridge IOD sensor block. Individual lanes can be invalid; the
        # reader decodes only lanes carrying the profile's explicit valid bit.
        iod_smn_temp_addresses=(0x59828, 0x5982C, 0x59834, 0x59838),
        iod_smn_temp_valid_bit=11, iod_smn_temp_field_shift=12,
        iod_smn_lane_numbers=(1, 2, 4, 5),
        # d[64] sits right after EDC_LIMIT (d[63]) and behaves like the
        # missing EDC_VALUE: idle ~7 A, rises to ~128 A under all-core load,
        # and stays above the same run's TDC current (d[9], ~108 A) as a
        # real peak-current reading should (research/recheck_edc.py).
        edc_value=64,
        prochot_smn_address=0x59804,
        prochot_ext_mask=0x04, prochot_cpu_mask=0x08, htc_mask=0x10,
        slot_count=16,
        requires_topology_mask=True,
        igpu_fields=(
            ("igpu_power", 107), ("igpu_clock", 108),
            ("igpu_busy", 110), ("igpu_idle", 187),
            ("igpu_temperature", 106), ("gpu_voltage", 105),
        ),
        global_fit=16,
    ),
}

WRITE_POLICIES = (
    WritePolicy(
        "AMD Ryzen 7 9800X3D", "AMD Ryzen 7 9800X3D", 0x620105, 1828, 8,
        ppt_msg=0x3E, tdc_msg=0x3C, edc_msg=0x3D,
        stock_ppt=162, stock_tdc=120, stock_edc=180,
        co_mode="legacy_per_message", thermal_msg=0x3F,
    ),
    WritePolicy(
        "AMD Ryzen 9 9950X3D", "AMD Ryzen 9 9950X3D", 0x620205, 2452, 16,
        ppt_msg=0x3E, tdc_msg=0x3C, edc_msg=0x3D,
        stock_ppt=200, stock_tdc=160, stock_edc=225,
        co_mode="packed_core_mask", co_msg=0x35, thermal_msg=0x3F,
    ),
)

# Candidate command layouts are intentionally separate from WRITE_POLICIES.
# The GUI may use one only after a per-session, user-visible risk confirmation;
# the global write gate remains closed for every unlisted model.
UNVALIDATED_CONTROL_POLICIES = {
    (0x620105, 1828): replace(WRITE_POLICIES[0], name="8-slot control candidate",
                              cpu_model="", cores=0),
    (0x620205, 2452): replace(WRITE_POLICIES[1], name="16-slot control candidate",
                              cpu_model="", cores=0),
}

# Opt-in integration-test mode.  It makes a known CPU exercise the same
# header-selected decoder used for an unlisted SKU, while keeping every mailbox
# write disabled.  It is intentionally an environment variable so ordinary
# launches retain the exact, validated profile.
FORCE_GENERIC_READ_PROFILE_ENV = "GNR_FORCE_GENERIC_READ_PROFILE"

# MP1 message IDs that must never be sent, wherever the send happens. This lived as a
# set literal in the CLI and as two separate ifs in the GUI, and the research tools had
# no equivalent at all — the same three-copies-of-one-rule shape that let the TDC/EDC
# mapping stay wrong in one copy for months.
#
#   0x03-0x0D, 0x10   dangerous MP1 IDs (docs/FINDINGS.md)
#   0x58-0x5D         freeze MP1 on this part: no response, recovery needs a reboot
BLOCKED_MSG_IDS = {0x10} | set(range(0x03, 0x0E)) | set(range(0x58, 0x5E))


def msg_id_blocked(msg_id):
    """(blocked, reason). Reason is None when the ID is allowed."""
    if 0x58 <= msg_id <= 0x5D:
        return True, (f"MSG 0x{msg_id:02x} freezes MP1 on Granite Ridge — no response, "
                      "recovery needs a reboot")
    if msg_id in BLOCKED_MSG_IDS:
        return True, f"MSG 0x{msg_id:02x} is on the never-send list"
    return False, None


_cached = None


def _core_count(cpuinfo="/proc/cpuinfo"):
    """Return physical cores from distinct (package, core-id) pairs."""
    try:
        pairs = set()
        package = "0"
        core = None
        with open(cpuinfo) as f:
            for line in f:
                if not line.strip():
                    if core is not None:
                        pairs.add((package, core))
                    package, core = "0", None
                elif line.startswith("physical id"):
                    package = line.split(":", 1)[1].strip()
                elif line.startswith("core id"):
                    core = line.split(":", 1)[1].strip()
        if core is not None:
            pairs.add((package, core))
        return len(pairs)
    except Exception:
        return 0


def _read_uint(path):
    with open(path, "rb") as f:
        data = f.read(8)
    if len(data) < 4:
        raise ValueError(f"short read ({len(data)} bytes)")
    return struct.unpack("<I", data[:4])[0]


def _cpu_model(cpuinfo="/proc/cpuinfo"):
    try:
        with open(cpuinfo) as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return ""


def detected_cpu_model(cpuinfo="/proc/cpuinfo"):
    """Return the Linux-reported CPU model for user-facing display."""
    return _cpu_model(cpuinfo) or "Unknown CPU"


def _cpu_signature(cpuinfo="/proc/cpuinfo"):
    """Return Linux's ``(family, model)`` pair, or ``(None, None)``.

    Generic PM profiles need this additional guard before using the Granite
    Ridge topology register.  A matching PM header alone is not permission to
    read a model-specific SMN address on another CPU family.
    """
    family = model = None
    try:
        with open(cpuinfo) as f:
            for line in f:
                key, separator, value = line.partition(":")
                if not separator:
                    continue
                if key.strip() == "cpu family":
                    family = int(value.strip(), 0)
                elif key.strip() == "model":
                    model = int(value.strip(), 0)
                if family is not None and model is not None:
                    break
    except (OSError, ValueError):
        pass
    return family, model


def _is_granite_ridge(cpuinfo="/proc/cpuinfo"):
    """Whether Linux identifies the current CPU as Granite Ridge desktop."""
    return _cpu_signature(cpuinfo) == (GRANITE_RIDGE_FAMILY,
                                       GRANITE_RIDGE_MODEL)


def _matching_write_policy(cpu_model, version, table_size, cores):
    """Return an exact model/header write authorization, if any.

    Firmware can disable individual physical slots or an entire CCD.  Linux
    then reports fewer physical cores although the installed, exact SKU and
    its PM-table format have not changed.  Core count is therefore a sanity
    ceiling rather than part of the model authorization.
    """
    for policy in WRITE_POLICIES:
        if (version, table_size) != (policy.pm_version, policy.table_size):
            continue
        if cores and cores > policy.cores:
            continue
        # Linux appends a core-count suffix. The required space makes this a
        # whole model-name match, so 9950X3D2 cannot match 9950X3D.
        if cpu_model.startswith(policy.cpu_model + " "):
            return policy
    return None


def _attach_write_policy(decoder, policy):
    """Return a format decoder with one separately validated write policy."""
    return replace(
        decoder,
        name=policy.name,
        cpu_model=policy.cpu_model,
        ppt_msg=policy.ppt_msg,
        tdc_msg=policy.tdc_msg,
        edc_msg=policy.edc_msg,
        stock_ppt=policy.stock_ppt,
        stock_tdc=policy.stock_tdc,
        stock_edc=policy.stock_edc,
        # ``cores`` is the geometric command range, not Linux's count of
        # currently enabled slots.  The GUI separately limits its controls to
        # the active-slot bitmap before constructing a CO command.
        cores=policy.cores,
        co_mode=policy.co_mode,
        co_msg=policy.co_msg,
        thermal_msg=policy.thermal_msg,
        allow_smu_writes=True,
        requires_topology_mask=False,
    )


def unvalidated_smu_control_profile(profile):
    """Attach a candidate control layout after an explicit GUI confirmation.

    This function does *not* authorize a write: ``allow_smu_writes`` remains
    false and callers must retain their own per-session confirmation state.
    It only supplies the format-matched message layout required to construct a
    command after the user deliberately accepts the warning.
    """
    if profile is None:
        return None
    candidate = UNVALIDATED_CONTROL_POLICIES.get(
        (profile.pm_version, profile.table_size))
    if candidate is None:
        return None
    return replace(
        profile,
        ppt_msg=candidate.ppt_msg,
        tdc_msg=candidate.tdc_msg,
        edc_msg=candidate.edc_msg,
        stock_ppt=candidate.stock_ppt,
        stock_tdc=candidate.stock_tdc,
        stock_edc=candidate.stock_edc,
        co_mode=candidate.co_mode,
        co_msg=candidate.co_msg,
        thermal_msg=candidate.thermal_msg,
        allow_smu_writes=False,
    )


def get_hardware_profile():
    """Return ``(profile_or_none, reason)``; cached for the process lifetime.

    Set ``GNR_FORCE_GENERIC_READ_PROFILE=1`` before process start to test the
    header-selected read-only decoder on an otherwise exact-profile CPU.
    """
    global _cached
    if _cached is not None:
        return _cached
    try:
        version = _read_uint(VERSION_PATH)
        table_size = _read_uint(SIZE_PATH)
    except Exception as e:
        _cached = (None, f"cannot read PM-table metadata ({e}) — is ryzen_smu loaded?")
        return _cached

    cores = _core_count()
    decoder = FORMAT_PROFILES.get((version, table_size))
    decoder_allowed = decoder is not None and _is_granite_ridge()
    force_generic = os.environ.get(FORCE_GENERIC_READ_PROFILE_ENV) == "1"
    cpu_model = _cpu_model()
    profile = replace(decoder, cores=cores) if decoder_allowed else None
    policy = (None if force_generic else
              _matching_write_policy(cpu_model, version, table_size, cores))
    if profile is not None and policy is not None:
        profile = _attach_write_policy(profile, policy)
    if profile is None:
        generic_note = ("; generic Granite Ridge decoder requires family 0x1a, "
                        "model 0x44" if decoder is not None else "")
        _cached = (
            None,
            f"unsupported PM table {hex(version)}, {table_size} bytes, "
            f"{cores or 'unknown'} physical cores, CPU {cpu_model or 'unknown'}"
            f"{generic_note}",
        )
        return _cached
    _cached = (
        profile,
        f"{profile.name}: PM table {hex(version)}, {table_size} bytes, {cores} cores"
        + (" (forced format decoder)" if force_generic else ""),
    )
    return _cached


def hardware_supported():
    """Compatibility API used by telemetry callers: ``(ok, reason)``."""
    profile, why = get_hardware_profile()
    return profile is not None, why


def smu_writes_supported():
    """Keep telemetry validation and mailbox-command validation separate."""
    profile, why = get_hardware_profile()
    if profile is None:
        return False, why
    if not profile.allow_smu_writes:
        return False, f"SMU writes are not validated on {profile.name}"
    return True, why


def smu_message_supported(profile, msg_id):
    """Only allow message IDs explicitly present in the selected profile."""
    allowed = {profile.ppt_msg, profile.tdc_msg, profile.edc_msg}
    if profile.thermal_msg is not None:
        allowed.add(profile.thermal_msg)
    if profile.co_mode == "legacy_per_message":
        allowed.update(range(0x50, 0x50 + profile.cores))
    elif profile.co_mode == "packed_core_mask":
        allowed.add(profile.co_msg)
    return msg_id in allowed


def curve_optimizer_command(profile, core, margin):
    """Return the profile-specific ``(MP1 message, arg0)`` for one physical core."""
    if not 0 <= core < profile.cores:
        raise ValueError(f"core {core} outside 0..{profile.cores - 1}")
    if not -50 <= margin <= 20:
        raise ValueError("Curve Optimizer margin must be between -50 and 20")
    if profile.co_mode == "legacy_per_message":
        return 0x50 + core, margin & 0xFFFFFFFF
    if profile.co_mode == "packed_core_mask":
        # Zen 3+: [31:28] CCD, [23:20] core-within-CCD, [15:0] signed margin.
        core_mask = (core // 8) << 28 | (core % 8) << 20
        return profile.co_msg, core_mask | (margin & 0xFFFF)
    raise ValueError(f"unsupported CO command mode: {profile.co_mode}")


def emulated_profile(cpu_model, pm_version, table_size):
    """Return a read-only decoder for an offline PM-table dump.

    The dump format selects telemetry offsets; the supplied CPU name is only
    the displayed identity. A captured table never authorizes mailbox writes.
    """
    # Keep CPU identity separate from the format decoder: different CPUs can
    # share a PM layout, while the captured data must never enable controls.
    decoder = FORMAT_PROFILES.get((pm_version, table_size))
    if decoder is None:
        raise ValueError(
            f"unsupported PM-table format {hex(pm_version)}, {table_size} bytes"
        )
    return replace(
        decoder,
        name=cpu_model,
        cpu_model=cpu_model,
        cores=decoder.slot_count or decoder.cores,
        allow_smu_writes=False,
        requires_topology_mask=False,
    )


def map_labels_supported():
    """The full PM_TABLE_MAP.md is currently the 9800X3D/457-float map."""
    profile, _ = get_hardware_profile()
    return (profile is not None
            and profile.cpu_model == "AMD Ryzen 7 9800X3D"
            and profile.pm_version == 0x620105)


if __name__ == "__main__":
    import tempfile

    # The parser is the only part worth checking without the hardware present. Use
    # real blank-line-separated processor blocks so the fixture matches /proc/cpuinfo.
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("processor\t: 0\ncpu family\t: 26\nmodel\t\t: 68\n"
                "physical id\t: 0\ncore id\t\t: 0\n\n"
                "processor\t: 1\nphysical id\t: 0\ncore id\t\t: 0\n\n"
                "processor\t: 2\nphysical id\t: 0\ncore id\t\t: 1\n")
        two_cores = f.name
    assert _core_count(two_cores) == 2, "two distinct physical cores"
    assert _core_count("/nonexistent") == 0, "unreadable cpuinfo must not claim a count"
    assert _is_granite_ridge(two_cores), "Granite Ridge family/model parser"

    for blocked_id in (0x03, 0x0D, 0x10, 0x58, 0x5D):
        assert msg_id_blocked(blocked_id)[0], f"0x{blocked_id:02x} must be blocked"
    for allowed_id in (0x02, 0x0E, 0x3C, 0x3D, 0x3E, 0x50, 0x57, 0x5E):
        assert not msg_id_blocked(allowed_id)[0], f"0x{allowed_id:02x} must be allowed"

    # Exercise format selection without coupling the self-test to a real SKU.
    emu = emulated_profile("Replay CPU", 0x620105, 1828)
    assert emu.cpu_model == "Replay CPU"
    assert emu.pm_version == 0x620105 and emu.table_size == 1828
    assert emu.cores == 8 and emu.ccd_count == 1
    assert not emu.allow_smu_writes

    for decoder in FORMAT_PROFILES.values():
        assert not decoder.allow_smu_writes, f"decoder must be read-only: {decoder.name}"
        assert decoder.requires_topology_mask, "unlisted model needs topology mask"
    for policy in WRITE_POLICIES:
        assert (policy.ppt_msg, policy.tdc_msg, policy.edc_msg) == (0x3E, 0x3C, 0x3D), \
            f"wrong power-limit mapping for {policy.name}"
        assert _matching_write_policy(
            policy.cpu_model + " 16-Core Processor", policy.pm_version,
            policy.table_size, policy.cores) == policy
    assert _matching_write_policy(
        "AMD Ryzen 9 9950X3D 16-Core Processor", 0x620205, 2452, 8) == WRITE_POLICIES[1]
    assert _matching_write_policy("AMD Ryzen 9 9950X3D2 16-Core Processor",
                                  0x620205, 2452, 16) is None
    for key, candidate in UNVALIDATED_CONTROL_POLICIES.items():
        decoder = replace(FORMAT_PROFILES[key], cores=FORMAT_PROFILES[key].slot_count)
        control_profile = unvalidated_smu_control_profile(decoder)
        assert control_profile is not None and not control_profile.allow_smu_writes
        assert smu_message_supported(control_profile, candidate.ppt_msg)
        assert smu_message_supported(control_profile, candidate.thermal_msg)

    profile, why = get_hardware_profile()
    print(f"{'SUPPORTED' if profile else 'REFUSED'}: {why}")
    print(f"this machine reports {_core_count()} physical cores")
    print(f"never-send list: {len(BLOCKED_MSG_IDS)} message IDs")
    if profile:
        print(f"per-core temperatures: d[{profile.core_temp}.."
              f"{profile.core_temp + profile.cores - 1}]")
        writes, write_why = smu_writes_supported()
        print(f"SMU writes: {'enabled' if writes else 'blocked'} ({write_why})")
