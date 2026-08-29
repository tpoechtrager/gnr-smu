# CPU documentation

CPU-specific telemetry and control documentation is grouped by architecture
and profile. See the [Zen 5 X3D documentation](zen5/README.md) for the
supported Ryzen 7 9800X3D and Ryzen 9 9950X3D profiles.

## Suspend/resume sample handling

After system standby or resume, the PM table can briefly return corrupt
temperature values (for example, extremely large positive or negative floats).
The live dashboard therefore discards the complete sample before updating the
display or its minimum/maximum/average statistics when any temperature is not
finite or lies outside `-300 … 1000 °C`. This check covers Tctl/Tdie, CCD,
per-core, thermal-limit, and L3-temperature fields. Values such as `-200 °C`
remain valid. A discarded sample leaves the last valid values and statistics
unchanged; the dashboard emits one warning for a contiguous invalid sequence.
