# Zen 5 X3D telemetry and controls

This directory documents the telemetry and controls used by the currently
supported Zen 5 X3D profiles:

| CPU | Profile documentation |
|---|---|
| Ryzen 7 9800X3D | [9800X3D.md](9800X3D.md) |
| Ryzen 9 9950X3D | [9950X3D.md](9950X3D.md) |

Each profile is enabled only when the detected CPU model, PM-table version,
table size, and physical-core count match its definition. Values and SMU
commands described here apply only to these two profiles.

All PM-table indices in this documentation are decimal float indices (`d[n]`).
The dashboard and exporter use the same profile selection, although the
exporter also includes several raw profile fields that are not shown in the
dashboard.

## Shared PM-table telemetry

### Limits and package telemetry

| Display or export field | Source | Unit |
|---|---:|---|
| CPU PPT Limit | `d[2]` | W |
| CPU PPT | `d[3]` | W |
| CPU TDC Limit | `d[8]` | A |
| CPU TDC | `d[9]` | A |
| Thermal Limit | `d[10]` | °C |
| Tctl/Tdie (exporter) | `d[11]` | °C |
| CPU EDC Limit | `d[63]` | A |
| CPU VID Limit | `d[18]` | V |
| CPU VID | `d[19]` | V |
| CPU SoC Power | `d[21]` | W |
| VDDIO MEM Power | `d[22]` | W |
| VDD18 Power | `d[23]` | W |

The dashboard uses the direct SMN Tctl/Tdie sensor described below. The
exporter additionally includes the PM-table Tctl/Tdie field at `d[11]`.
Package-power fields whose source differs between the two profiles are listed
on the individual profile pages.

### Clocks and voltage rails

| Display or export field | Source | Unit |
|---|---:|---|
| Infinity Fabric Clock (FCLK) | `d[71]` | MHz |
| Memory Controller Clock (UCLK) | `d[75]` | MHz |
| Memory Clock (MCLK) | `d[79]` | MHz |
| VDD_MISC | `d[58]` | V |
| VDDCR_SOC | `d[83]` | V |
| CLDO_VDDG_IOD | `d[259]` | V |
| CLDO_VDDG_CCD | `d[261]` | V |
| CLDO_VDDP | `d[269]` | V |
| Vcore Peak | maximum of the profile's core-voltage lanes | V |
| Vcore Average | average of the profile's core-voltage lanes | V |

## Direct SMN telemetry

The dashboard uses direct SMN access for the following fields. The telemetry
exporter uses it for the thermal-status and IOD fields; its Tctl/Tdie value is
the PM-table field listed above.

| Sensor | Source |
|---|---|
| Tctl/Tdie | SMN `0x59800` |
| Thermal Throttling (PROCHOT EXT) | SMN `0x59804`, bit 2 (`0x04`) |
| Thermal Throttling (PROCHOT CPU) | SMN `0x59804`, bit 3 (`0x08`) |
| Thermal Throttling (HTC) | SMN `0x59804`, bit 4 (`0x10`) |

The available CCD sensors are listed in the corresponding profile page.

### IOD lanes

Both profiles support the following IOD temperature lanes:

| Lane | SMN address |
|---:|---:|
| 1 | `0x59828` |
| 2 | `0x5982C` |
| 4 | `0x59834` |
| 5 | `0x59838` |

An IOD value is displayed only when its validity bit is set. The temperature
field uses bits 12–23, a scale of 0.125 °C, and a -49 °C offset.

Direct SMN access requires root privileges. When started without root, the GUI
retains PM-table telemetry and hides Tctl/Tdie, CCD, IOD, and thermal-status
sensors. The named CSV exporter marks unavailable SMN values accordingly; the
default JSON mode contains raw PM-table snapshots only.

## Dashboard groups and derived values

The dashboard groups PPT, TDC, and EDC under **CPU Limits → Package Power &
Current**. **Thermal** contains the configured thermal limit together with the
PROCHOT and HTC status rows.

**Core Power Sum** is calculated as the sum of the active profile's per-core
power lanes. CCD residency summaries are averages of their cores' residency
lanes. The per-core details and the C0 calculation are profile-specific and
are documented on the respective profile page.

The **Configured Curve Optimizer** group displays the offsets stored by GNR
Master in its local configuration. It is not a read-back of the current SMU
state.

## Common controls

Both profiles allow the following MP1 controls:

| Control | MP1 message | Argument |
|---|---:|---|
| PPT | `0x3E` | mW |
| TDC | `0x3C` | mA |
| EDC | `0x3D` | mA |
| Thermal Limit | `0x3F` | whole °C |

Curve Optimizer uses a profile-specific command format and is documented on the
respective profile page.
