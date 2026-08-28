#!/usr/bin/env python3
"""Hardware profiles and safety gates for Granite Ridge PM-table tools.

Telemetry layouts are keyed by PM-table version, byte size and physical core count.
SMU writes have a separate, stricter gate: validating read-only telemetry on a CPU
does not establish that mailbox commands or Curve Optimizer IDs are safe on it.
"""

from dataclasses import dataclass
import struct
from typing import Optional

VERSION_PATH = "/sys/kernel/ryzen_smu_drv/pm_table_version"
SIZE_PATH = "/sys/kernel/ryzen_smu_drv/pm_table_size"


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
    core_c0: Optional[int]
    core_cc1: Optional[int]
    core_cc6: int
    core_boost_limit: int
    boost_limit_confident: bool
    # Unconfirmed CCD-adjacent candidates: table position suggested "L3", but
    # a cache-thrash-vs-ALU comparison (research/l3_specificity.py) has not
    # proven L3-cache coupling, so these are named for what was actually
    # measured (CCD selectivity), not for an unproven L3 identity.
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
    ccd_candidate_count: int
    ppt_msg: int
    tdc_msg: int
    edc_msg: int
    stock_ppt: int
    stock_tdc: int
    stock_edc: int
    co_mode: str
    co_msg: int = 0
    allow_smu_writes: bool = False
    # Direct Tctl/Tdie register. Like the CCD registers below, this is separate
    # from the PM table and is never inferred for an unknown profile.
    tctl_smn_address: Optional[int] = None
    # Direct CCD temperature registers. These are read-only SMN locations, separate
    # from the PM table. They are explicit per-profile so an unknown CPU never gets
    # a guessed raw-SMN address.
    ccd_smn_temp_addresses: tuple = ()
    ccd_shared_temperature: Optional[int] = None
    edc_value: Optional[int] = None

    @property
    def float_count(self):
        return self.table_size // 4


PROFILES = {
    (0x620105, 1828, 8): HardwareProfile(
        "AMD Ryzen 7 9800X3D", "AMD Ryzen 7 9800X3D", 0x620105, 1828, 8,
        core_power=333, core_voltage=309, core_temp=317, core_frequency=325,
        core_fit=341, core_activity=357, core_c0=None, core_cc1=None,
        core_cc6=349, core_boost_limit=373, boost_limit_confident=True,
        ccd_power_candidate=None, ccd_vddm_candidate=None,
        ccd_l3_temperature=None, ccd_candidate_count=0,
        # Confirmed by read-back in research/probe_tdc_edc.py.
        ppt_msg=0x3E, tdc_msg=0x3C, edc_msg=0x3D,
        stock_ppt=162, stock_tdc=120, stock_edc=180,
        co_mode="legacy_per_message",
        allow_smu_writes=True,
        tctl_smn_address=0x59800,
        # Linux k10temp maps Zen 5 Ryzen Desktop Tccd1 to 0x59800 + 0x308.
        ccd_smn_temp_addresses=(0x59B08,),
    ),
    (0x620205, 2452, 16): HardwareProfile(
        "AMD Ryzen 9 9950X3D", "AMD Ryzen 9 9950X3D", 0x620205, 2452, 16,
        core_power=301, core_voltage=317, core_temp=333, core_frequency=349,
        # Ryzen Master multiplies d[349+i] by 1000 before exposing its
        # per-core clock array. Live comparison on this 9950X3D found
        # d[349] = 5.472 GHz while Linux reported 5456 MHz for core 0.
        # Do not retain the previous FIT label for this frequency lane.
        core_fit=None, core_activity=365, core_c0=381, core_cc1=397,
        core_cc6=413, core_boost_limit=445, boost_limit_confident=False,
        # Low-confidence candidates from the 0x620205 table.  Keep these
        # explicitly separate from validated fields until correlated traces
        # establish their identities.
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
        ccd_l3_temperature=595, ccd_candidate_count=2,
        # ZenStates-Core's Granite Ridge profile inherits the Zen 4 MP1 command
        # table: Fast/PPT=0x3E, TDC=0x3C, EDC=0x3D, per-core DLDO margin=0x35.
        ppt_msg=0x3E, tdc_msg=0x3C, edc_msg=0x3D,
        stock_ppt=200, stock_tdc=160, stock_edc=225,
        co_mode="packed_core_mask", co_msg=0x35,
        allow_smu_writes=True,
        tctl_smn_address=0x59800,
        # Same Zen 5 Desktop register block: Tccd1, Tccd2.
        ccd_smn_temp_addresses=(0x59B08, 0x59B0C),
        # d[64] sits right after EDC_LIMIT (d[63]) and behaves like the
        # missing EDC_VALUE: idle ~7 A, rises to ~128 A under all-core load,
        # and stays above the same run's TDC current (d[9], ~108 A) as a
        # real peak-current reading should (research/recheck_edc.py).
        edc_value=64,
    ),
}

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


def get_hardware_profile():
    """Return ``(profile_or_none, reason)``; cached for the process lifetime."""
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
    profile = PROFILES.get((version, table_size, cores))
    cpu_model = _cpu_model()
    if profile is not None and profile.cpu_model not in cpu_model:
        profile = None
    if profile is None:
        _cached = (
            None,
            f"unsupported PM table {hex(version)}, {table_size} bytes, "
            f"{cores or 'unknown'} physical cores, CPU {cpu_model or 'unknown'}",
        )
        return _cached
    _cached = (
        profile,
        f"{profile.name}: PM table {hex(version)}, {table_size} bytes, {cores} cores",
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


def map_labels_supported():
    """The full PM_TABLE_MAP.md is currently the 9800X3D/457-float map."""
    profile, _ = get_hardware_profile()
    return profile is not None and profile.pm_version == 0x620105


if __name__ == "__main__":
    import tempfile

    # The parser is the only part worth checking without the hardware present. Use
    # real blank-line-separated processor blocks so the fixture matches /proc/cpuinfo.
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("processor\t: 0\nphysical id\t: 0\ncore id\t\t: 0\n\n"
                "processor\t: 1\nphysical id\t: 0\ncore id\t\t: 0\n\n"
                "processor\t: 2\nphysical id\t: 0\ncore id\t\t: 1\n")
        two_cores = f.name
    assert _core_count(two_cores) == 2, "two distinct physical cores"
    assert _core_count("/nonexistent") == 0, "unreadable cpuinfo must not claim a count"

    for blocked_id in (0x03, 0x0D, 0x10, 0x58, 0x5D):
        assert msg_id_blocked(blocked_id)[0], f"0x{blocked_id:02x} must be blocked"
    for allowed_id in (0x02, 0x0E, 0x3C, 0x3D, 0x3E, 0x50, 0x57, 0x5E):
        assert not msg_id_blocked(allowed_id)[0], f"0x{allowed_id:02x} must be allowed"

    for profile_key, test_profile in PROFILES.items():
        assert (test_profile.ppt_msg, test_profile.tdc_msg, test_profile.edc_msg) == \
            (0x3E, 0x3C, 0x3D), f"wrong power-limit mapping for {test_profile.name}"

    profile, why = get_hardware_profile()
    print(f"{'SUPPORTED' if profile else 'REFUSED'}: {why}")
    print(f"this machine reports {_core_count()} physical cores")
    print(f"never-send list: {len(BLOCKED_MSG_IDS)} message IDs")
    if profile:
        print(f"per-core temperatures: d[{profile.core_temp}.."
              f"{profile.core_temp + profile.cores - 1}]")
        writes, write_why = smu_writes_supported()
        print(f"SMU writes: {'enabled' if writes else 'blocked'} ({write_why})")
