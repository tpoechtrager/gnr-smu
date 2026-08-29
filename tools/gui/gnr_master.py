import sys
import struct
import subprocess
import json
import os
import math
from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import *

CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "gnr_master.json",
)
SMN_PATH = "/sys/kernel/ryzen_smu_drv/smn"
SMU_ARGS_PATH = "/sys/kernel/ryzen_smu_drv/smu_args"
SMU_CMD_PATH = "/sys/kernel/ryzen_smu_drv/mp1_smu_cmd"

# Keep this deliberately broad: values such as -200 C remain valid, while
# corrupt PM-table values after suspend/resume discard the complete snapshot.
TEMPERATURE_SAMPLE_MIN_C = -300.0
TEMPERATURE_SAMPLE_MAX_C = 1000.0

# ``hwgate`` owns PM offsets and the exact write gate. Unlisted models may
# expose a format-matched candidate layout only after a per-session warning.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hwgate import (curve_optimizer_command, get_hardware_profile,
                    detected_cpu_model, hardware_supported, msg_id_blocked,
                    smu_message_supported,
                    smu_writes_supported, unvalidated_smu_control_profile)  # noqa: E402
from smn_telemetry import (read_profile_active_core_slots, read_profile_iod_lanes,
                           read_profile_prochot_status)  # noqa: E402

# --- Color Theme ---
BG_MAIN = "#101722"
BG_SIDEBAR = "#0b111b"
BG_PANEL = "#172233"
BG_INNER = "#1d2a3d"
BG_TREE = "#0b1018"
BG_TREE_ALT = "#121b27"
BG_TREE_CURRENT = "#193451"
BORDER = "#2c3c52"
TEXT_MAIN = "#e6edf3"
TEXT_MUTED = "#91a4bd"
ACCENT_RED = "#f05d68"
ACCENT_ORANGE = "#ff9d2e"
ACCENT_GREEN = "#36c987"
ACCENT_PURPLE = "#b48cff"
ACCENT_CYAN = "#42c8e8"


def decode_zen_tctl_smn_temperature(raw):
    """Decode Zen's reported Tctl/Tdie SMN register."""
    temp = (raw >> 21) * 0.125
    # These flags select the -49 C temperature range in the kernel's k10temp
    # decoder. Keep the exact register semantics here rather than deriving a
    # temperature from a PM-table field.
    if raw & (1 << 19) or (raw & 0x30000) == 0x30000:
        temp -= 49.0
    return temp


def decode_ccd_smn_temperature(raw):
    """Decode the Zen CCD-temperature register."""
    if not raw & (1 << 11):
        return None
    return (raw & 0x7FF) * 0.125 - 49.0


def read_smn_u32(address):
    """Perform one read-only SMN transaction through ryzen_smu's locked interface."""
    try:
        # The module treats precisely one little-endian uint32 as an SMN *read*
        # address. Two words would be an SMN write; never use that form here.
        with open(SMN_PATH, "wb") as stream:
            stream.write(struct.pack("<I", address))
        with open(SMN_PATH, "rb") as stream:
            raw = stream.read(4)
        if len(raw) != 4:
            return None
        return struct.unpack("<I", raw)[0]
    except OSError:
        return None


def read_profile_ccd_temperature(profile, ccd):
    """Read an exact, profile-approved CCD sensor; return None if unavailable."""
    if profile is None or not 0 <= ccd < len(profile.ccd_smn_temp_addresses):
        return None
    raw = read_smn_u32(profile.ccd_smn_temp_addresses[ccd])
    return decode_ccd_smn_temperature(raw) if raw is not None else None


def read_profile_tctl_temperature(profile):
    """Read the exact, profile-approved Tctl/Tdie sensor or return None."""
    if profile is None or profile.tctl_smn_address is None:
        return None
    raw = read_smn_u32(profile.tctl_smn_address)
    return decode_zen_tctl_smn_temperature(raw) if raw is not None else None


def smu_write_permission_reason():
    """Return a human-readable preflight failure, or None when writes are allowed."""
    paths = (SMU_ARGS_PATH, SMU_CMD_PATH)
    missing = [path for path in paths if not os.path.exists(path)]
    if missing:
        return "SMU driver interface is unavailable. Is ryzen_smu loaded?"
    unavailable = [path for path in paths if not os.access(path, os.W_OK)]
    if unavailable:
        return ("SMU controls require root privileges. Start GNR Master with "
                "sudo to change power limits or Curve Optimizer settings.")
    return None


# ================= THREAD : REAL-TIME KERNEL LOGS =================
class KernelLogWorker(QThread):
    log_signal = pyqtSignal(str)

    def run(self):
        try:
            process = subprocess.Popen(
                ["dmesg", "-w"], stdout=subprocess.PIPE, text=True
            )
            for line in iter(process.stdout.readline, ""):
                if "gnr_smu" in line or "ryzen_smu" in line:
                    clean_line = (
                        line.split("] ", 1)[-1].strip()
                        if "] " in line
                        else line.strip()
                    )
                    self.log_signal.emit(clean_line)
        except Exception:
            pass


# ================= CONTROL DIALOGS =================
class PowerControlDialog(QDialog):
    def __init__(self, cur_ppt, cur_tdc, cur_edc, cur_thermal, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Power & Thermal Controls")
        self.setStyleSheet(
            f"background-color: {BG_MAIN}; color: {TEXT_MAIN}; font-family: 'Segoe UI';"
        )
        self.setFixedSize(320, 300)
        layout = QVBoxLayout(self)
        self.inputs = {}
        configs = [
            ("PPT", 250, "W", cur_ppt),
            ("TDC", 200, "A", cur_tdc),
            ("EDC", 250, "A", cur_edc),
            ("Thermal Limit", 100, "°C", cur_thermal),
        ]

        for name, max_val, unit, current_val in configs:
            row = QHBoxLayout()
            lbl = QLabel(name)
            lbl.setStyleSheet("font-weight: bold; width: 40px;")
            spin = QDoubleSpinBox()
            # Keep the thermal control in a conservative, useful range.  The
            # SMU command takes whole degrees Celsius; values above 100 °C
            # would weaken the silicon's thermal protection.
            if name == "Thermal Limit":
                # AMD's published HTC control rejects values below 52 °C.
                spin.setRange(52, max_val)
                spin.setDecimals(0)
                spin.setSingleStep(1)
            else:
                spin.setRange(0, max_val)
            spin.setSuffix(f" {unit}")
            spin.setStyleSheet(
                f"background-color: {BG_PANEL}; border: 1px solid {BORDER}; padding: 5px;"
            )
            spin.setValue(current_val)
            self.inputs[name] = spin
            row.addWidget(lbl)
            row.addWidget(spin)
            layout.addLayout(row)

        btn_apply = QPushButton("Apply Limits (MP1)")
        btn_apply.setStyleSheet(
            f"background-color: {ACCENT_RED}; color: white; border-radius: 4px; padding: 8px; font-weight: bold;"
        )
        btn_apply.clicked.connect(self.accept)
        layout.addStretch()
        layout.addWidget(btn_apply)


class CoreControlDialog(QDialog):
    def __init__(self, current_co_offsets, core_slots, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Curve Optimizer (CO)")
        self.setStyleSheet(
            f"background-color: {BG_MAIN}; color: {TEXT_MAIN}; font-family: 'Segoe UI';"
        )
        height = 400 if len(core_slots) <= 8 else 560
        self.setFixedSize(350, height)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Set Curve Optimizer Offsets per Core:"))
        self.spins = {}
        grid = QGridLayout()
        for row, core in enumerate(core_slots):
            lbl = QLabel(f"Core {core}:")
            spin = QSpinBox()
            spin.setRange(-50, 20)
            spin.setStyleSheet(
                f"background-color: {BG_PANEL}; border: 1px solid {BORDER}; padding: 3px;"
            )
            spin.setValue(current_co_offsets[core])
            self.spins[core] = spin
            grid.addWidget(lbl, row // 2, (row % 2) * 2)
            grid.addWidget(spin, row // 2, (row % 2) * 2 + 1)

        layout.addLayout(grid)
        btn_apply = QPushButton("Apply Curve Optimizer")
        btn_apply.setStyleSheet(
            f"background-color: {ACCENT_ORANGE}; color: white; border-radius: 4px; padding: 8px; font-weight: bold;"
        )
        btn_apply.clicked.connect(self.accept)
        layout.addStretch()
        layout.addWidget(btn_apply)


class SettingsDialog(QDialog):
    """Persistent display and sampling preferences."""

    def __init__(self, refresh_ms, show_disabled_cores, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setStyleSheet(
            f"QDialog {{ background-color: {BG_MAIN}; color: {TEXT_MAIN}; "
            "font-family: 'Segoe UI'; }"
            f"QLabel {{ background: transparent; color: {TEXT_MAIN}; }}"
            f"QFrame#settingsSection {{ background: {BG_PANEL}; border: 1px solid {BORDER}; "
            "border-radius: 7px; }"
            f"QSpinBox {{ background: {BG_INNER}; border: 1px solid {BORDER}; "
            f"color: {TEXT_MAIN}; border-radius: 5px; padding: 5px 8px; }}"
            f"QSpinBox:focus {{ border-color: {ACCENT_CYAN}; }}"
            f"QCheckBox {{ background: transparent; color: {TEXT_MAIN}; font-size: 12px; font-weight: 650; }}"
            f"QPushButton#settingsCancel {{ background: transparent; border: 1px solid {BORDER}; "
            f"border-radius: 5px; color: {TEXT_MAIN}; padding: 7px 15px; min-width: 84px; }}"
            f"QPushButton#settingsCancel:hover {{ border-color: {ACCENT_CYAN}; }}"
            f"QPushButton#settingsApply {{ background: {ACCENT_ORANGE}; border: 1px solid {ACCENT_ORANGE}; "
            "border-radius: 5px; color: #101722; padding: 7px 15px; min-width: 104px; "
            "font-weight: 700; }"
            f"QPushButton#settingsApply:hover {{ background: #ffb34d; border-color: #ffb34d; }}"
        )
        self.setFixedSize(440, 330)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = QLabel("Monitoring settings")
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #f8fafc;")
        layout.addWidget(title)
        subtitle = QLabel("Sampling interval and optional diagnostic rows")
        subtitle.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(subtitle)

        sampling = QFrame()
        sampling.setObjectName("settingsSection")
        sampling_layout = QVBoxLayout(sampling)
        sampling_layout.setContentsMargins(13, 10, 13, 10)
        sampling_layout.setSpacing(7)

        telemetry_heading = QLabel("SAMPLING")
        telemetry_heading.setStyleSheet(
            f"background: transparent; color: {ACCENT_CYAN}; font-size: 10px; "
            "font-weight: 800; letter-spacing: 1px;"
        )
        sampling_layout.addWidget(telemetry_heading)
        refresh_row = QHBoxLayout()
        refresh_row.setSpacing(12)
        refresh_text = QVBoxLayout()
        refresh_text.setSpacing(1)
        refresh_label = QLabel("Refresh rate")
        refresh_label.setStyleSheet("background: transparent; font-size: 12px; font-weight: 650;")
        refresh_hint = QLabel("How often the dashboard updates")
        refresh_hint.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 10px;")
        refresh_text.addWidget(refresh_label)
        refresh_text.addWidget(refresh_hint)
        refresh_row.addLayout(refresh_text, 1)
        self.refresh_input = QSpinBox()
        self.refresh_input.setRange(100, 10000)
        self.refresh_input.setSingleStep(100)
        self.refresh_input.setSuffix(" ms")
        self.refresh_input.setValue(refresh_ms)
        self.refresh_input.setToolTip("Telemetry refresh interval.")
        self.refresh_input.setMinimumWidth(124)
        refresh_row.addWidget(self.refresh_input)
        sampling_layout.addLayout(refresh_row)
        layout.addWidget(sampling)

        display = QFrame()
        display.setObjectName("settingsSection")
        display_layout = QVBoxLayout(display)
        display_layout.setContentsMargins(13, 10, 13, 10)
        display_layout.setSpacing(5)

        display_heading = QLabel("DISPLAY")
        display_heading.setStyleSheet(
            f"background: transparent; color: {ACCENT_CYAN}; font-size: 10px; "
            "font-weight: 800; letter-spacing: 1px;"
        )
        display_layout.addWidget(display_heading)

        self.show_disabled_cores = QCheckBox("Show inactive Cores and CCDs")
        self.show_disabled_cores.setObjectName("showInactiveSlots")
        self.show_disabled_cores.setChecked(show_disabled_cores)
        self.show_disabled_cores.setToolTip(
            "Show Cores and CCDs that are unavailable on this CPU, including "
            "lower-core-count variants and Cores or CCDs disabled in BIOS. "
            "Values can be stale or zero."
        )

        hint = QLabel(
            "Includes Cores absent on lower-core-count CPUs (for example, 6-core "
            "models) and Cores or CCDs disabled in BIOS. Raw values may be retained or zero."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"background: transparent; color: {TEXT_MUTED}; font-size: 10px;")
        display_layout.addWidget(self.show_disabled_cores)
        display_layout.addWidget(hint)
        layout.addWidget(display)

        actions = QHBoxLayout()
        actions.addStretch()
        cancel_button = QPushButton("Cancel")
        cancel_button.setObjectName("settingsCancel")
        cancel_button.clicked.connect(self.reject)
        apply_button = QPushButton("Save settings")
        apply_button.setObjectName("settingsApply")
        apply_button.clicked.connect(self.accept)
        apply_button.setDefault(True)
        actions.addWidget(cancel_button)
        actions.addWidget(apply_button)
        layout.addLayout(actions)


# ================= UI COMPONENTS =================
class TelemetryPanel(QFrame):
    def __init__(self, title, subtitle=""):
        super().__init__()
        self.setObjectName("telemetryPanel")
        self.setStyleSheet(
            f"QFrame#telemetryPanel {{ background: {BG_PANEL}; border: 1px solid {BORDER}; "
            "border-radius: 9px; } QLabel { border: none; background: transparent; }"
        )
        self.layout_box = QVBoxLayout(self)
        self.layout_box.setContentsMargins(15, 13, 15, 13)
        self.layout_box.setSpacing(8)
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet("font-size: 14px; font-weight: 700; color: #e2e8f0;")
        self.layout_box.addWidget(title_lbl)
        if subtitle:
            subtitle_lbl = QLabel(subtitle)
            subtitle_lbl.setWordWrap(True)
            subtitle_lbl.setStyleSheet(f"font-size: 10px; color: {TEXT_MUTED};")
            self.layout_box.addWidget(subtitle_lbl)

    def add_row(self, text="--", color=TEXT_MAIN):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color: {color}; font-size: 12px; padding: 7px 8px; "
            f"background: {BG_INNER}; border-radius: 5px;"
        )
        self.layout_box.addWidget(label)
        return label


# ================= APPLICATION PRINCIPALE =================
class GNRMaster(QMainWindow):
    def __init__(self):
        super().__init__()
        self._smn_readable = bool(getattr(os, "geteuid", lambda: 0)() == 0)
        self.cpu_name = detected_cpu_model()
        self.profile, self.profile_reason = get_hardware_profile()
        self.config = self._load_config()
        self.show_disabled_cores = bool(self.config.get("show_disabled_cores", False))
        # Never persisted: unvalidated control requires a fresh, explicit
        # acknowledgement every time the application starts.
        self._unvalidated_smu_override = False
        slots = read_profile_active_core_slots(self.profile)
        if slots is None and self.profile and self.profile.requires_topology_mask:
            self.profile = None
            self.profile_reason = ("generic Granite Ridge read support requires root "
                                   "to read the active-slot SMN mask")
            slots = ()
        if self.profile is None:
            self.core_slots = ()
        elif slots is None:
            # Exact profiles have an established dense topology fallback.
            self.core_slots = tuple(range(self.profile.slot_count or self.profile.cores))
        else:
            self.core_slots = slots
        self.core_count = len(self.core_slots)
        # A PM format describes the maximum geometric CCD width.  The UI must
        # use only CCDs with at least one active physical PM slot; otherwise a
        # disabled CCD would still appear as a permanent ``--`` row.
        self.active_ccds = tuple(sorted({slot // 8 for slot in self.core_slots}))
        self._refresh_display_topology()
        cpu_name = self.profile.name if self.profile else "Unsupported CPU"
        self.setWindowTitle(f"GNR Master - {cpu_name} Telemetry")
        self.setMinimumSize(980, 620)
        self.setStyleSheet(
            f"background-color: {BG_MAIN}; color: {TEXT_MAIN}; font-family: 'Segoe UI';"
        )

        self.sensor_update_ms = self._sanitize_update_interval(
            self.config.get("sensor_update_ms", 500)
        )
        self._preferences_timer = QTimer(self)
        self._preferences_timer.setSingleShot(True)
        self._preferences_timer.timeout.connect(self._save_sensor_preferences)
        (self.current_ppt, self.current_tdc, self.current_edc,
         self.current_thermal) = self._read_pm_limits()
        # The SMU write interface has no verified CO readback path. Keep values
        # only for this process so the UI never presents a local cache as live
        # hardware configuration.
        slot_count = self.profile.slot_count if self.profile else self.core_count
        self.current_co = [0] * slot_count

        self._build_interface()
        self._restore_window_size()
        self._show_unprivileged_warning()
        self._temperature_sample_discarded = False

        self.log_msg(
            "Dashboard initialized. Listening to kernel logs...", "STATUS", ACCENT_GREEN
        )

        self.log_worker = KernelLogWorker()
        self.log_worker.log_signal.connect(
            lambda msg: self.log_msg(msg, "KERNEL", ACCENT_PURPLE)
        )
        self.log_worker.start()

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_data)
        self.timer.start(self.sensor_update_ms)

    def _build_interface(self):
        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.setCentralWidget(root)

        sidebar = QFrame()
        self.sidebar_width = 138
        sidebar.setFixedWidth(self.sidebar_width)
        sidebar.setStyleSheet(
            f"background: {BG_SIDEBAR}; border-right: 1px solid {BORDER};"
        )
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(10, 16, 10, 16)
        sidebar_layout.setSpacing(7)
        brand = QLabel("GNR\nMASTER")
        brand.setStyleSheet(
            f"color: {ACCENT_ORANGE}; font-size: 16px; font-weight: 800; "
            "letter-spacing: 2px; padding: 2px 8px 18px 8px;"
        )
        sidebar_layout.addWidget(brand)
        self.nav_buttons = []
        self.pages = QStackedWidget()
        page_specs = [
            ("Dashboard", "◫", self._build_overview_page),
            ("Logs", "≡", self._build_logs_page),
        ]
        for index, (name, icon, builder) in enumerate(page_specs):
            button = QPushButton(f"{icon}  {name}")
            button.setCheckable(True)
            button.setMinimumHeight(42)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked, i=index: self._show_page(i))
            self.nav_buttons.append(button)
            sidebar_layout.addWidget(button)
            self.pages.addWidget(builder())
        sidebar_layout.addStretch()
        controls = QLabel("CONTROLS")
        controls.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 9px; font-weight: 700; padding: 0 8px;"
        )
        sidebar_layout.addWidget(controls)
        core_control = QPushButton("⚡  Core Control")
        power_control = QPushButton("⌁  Power / Thermal")
        settings_control = QPushButton("⚙  Settings")
        for button in (core_control, power_control, settings_control):
            button.setMinimumHeight(42)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(self._control_button_style())
            sidebar_layout.addWidget(button)
        core_control.clicked.connect(self.open_core_control)
        power_control.clicked.connect(self.open_power_control)
        settings_control.clicked.connect(self.open_settings)
        root_layout.addWidget(sidebar)
        root_layout.addWidget(self.pages, 1)
        self._show_page(0)

    def _page(self, title, subtitle):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 17, 20, 17)
        layout.setSpacing(13)
        heading = QLabel(title)
        heading.setStyleSheet("font-size: 22px; font-weight: 750; color: #f8fafc;")
        layout.addWidget(heading)
        if subtitle:
            caption = QLabel(subtitle)
            caption.setStyleSheet(f"font-size: 11px; color: {TEXT_MUTED};")
            layout.addWidget(caption)
        return page, layout

    def _build_overview_page(self):
        page, layout = self._page("Dashboard", "")
        layout.setSpacing(8)
        status_bar = QFrame()
        status_bar.setObjectName("statusBar")
        status_bar.setStyleSheet(
            f"QFrame#statusBar {{ background: {BG_PANEL}; border: 1px solid {BORDER}; "
            "border-radius: 4px; }"
        )
        status_bar.setMinimumHeight(42)
        status_layout = QHBoxLayout(status_bar)
        status_layout.setContentsMargins(12, 5, 9, 5)
        status_layout.setSpacing(8)
        self.summary_values = {}
        self.summary_widgets = {}
        cpu_summary = self._add_summary_metric(status_layout, "cpu", "CPU", ACCENT_ORANGE)
        ccd_summaries = []
        for ccd in self.visible_ccds:
            ccd_summaries.append(
                self._add_summary_metric(
                    status_layout, f"ccd{ccd}", self._ccd_label(ccd), ACCENT_CYAN
                )
            )
        if not self._smn_readable:
            cpu_summary.hide()
            for summary in ccd_summaries:
                summary.hide()
        self._add_summary_separator(status_layout)
        self._add_summary_metric(status_layout, "frequency", "FREQ", ACCENT_PURPLE)
        self._add_summary_separator(status_layout)
        self._add_summary_metric(status_layout, "limits", "", TEXT_MAIN, minimum_width=270)
        status_layout.addStretch()
        self.reset_stats_button = QPushButton("Reset min/max")
        self.reset_stats_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_stats_button.setMinimumHeight(32)
        self.reset_stats_button.setStyleSheet(
            f"QPushButton {{ color: #cbd5e1; background: {BG_PANEL}; border: 1px solid {BORDER}; "
            "border-radius: 6px; padding: 6px 16px; font-size: 12px; font-weight: 600; }} "
            f"QPushButton:hover {{ border-color: {ACCENT_ORANGE}; color: {TEXT_MAIN}; }}"
        )
        self.reset_stats_button.clicked.connect(self._reset_sensor_stats)
        status_layout.addWidget(self.reset_stats_button)
        layout.addWidget(status_bar)

        self.sensor_tree = QTreeWidget()
        self.sensor_tree.setHeaderLabels(["Sensor", "Current", "Minimum", "Maximum", "Average"])
        self.sensor_tree.setRootIsDecorated(True)
        self.sensor_tree.setAlternatingRowColors(True)
        self.sensor_tree.setUniformRowHeights(True)
        self.sensor_tree.setIndentation(16)
        self.sensor_tree.setAnimated(False)
        self.sensor_tree.setStyleSheet(
            f"QTreeWidget {{ background: {BG_TREE}; alternate-background-color: {BG_TREE_ALT}; "
            f"color: {TEXT_MAIN}; border: 1px solid {BORDER}; font-family: Consolas, monospace; "
            "font-size: 12px; } "
            "QTreeWidget::item { height: 22px; border-bottom: 1px solid #172231; } "
            "QTreeWidget::item:selected { background: #214364; color: #ffffff; } "
            f"QHeaderView::section {{ background: {BG_PANEL}; color: {TEXT_MAIN}; border: none; "
            f"border-right: 1px solid {BORDER}; padding: 5px 8px; font-weight: 700; }}"
        )
        header = self.sensor_tree.header()
        header.setStretchLastSection(False)
        header.setSectionsMovable(True)
        header.setSectionsClickable(True)
        header.setMinimumSectionSize(80)
        header.setToolTip("Drag column headers to reorder them; drag separators to resize.")
        for column in range(5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        self.sensor_tree.setColumnWidth(0, 620)
        for column in range(1, 5):
            self.sensor_tree.setColumnWidth(column, 145)
        saved_header = self.config.get("sensor_header_state")
        if isinstance(saved_header, str):
            try:
                header.restoreState(QByteArray.fromBase64(saved_header.encode("ascii")))
            except UnicodeEncodeError:
                pass
        self.sensor_items = {}
        self.sensor_units = {}
        self.sensor_stats = {}
        self.sensor_groups = {}
        self._build_sensor_tree()
        header.sectionMoved.connect(self._queue_sensor_preferences)
        header.sectionResized.connect(self._queue_sensor_preferences)
        self.sensor_tree.itemExpanded.connect(self._queue_sensor_preferences)
        self.sensor_tree.itemCollapsed.connect(self._queue_sensor_preferences)
        layout.addWidget(self.sensor_tree)
        return page

    def _show_unprivileged_warning(self):
        """Explain why direct SMN sensors are hidden without root privileges."""
        suppress_warning = self.config.get("suppress_unprivileged_warning")
        if suppress_warning is None:
            # Migrate the short-lived inverse setting used by an earlier dialog
            # version without changing the default for existing configurations.
            suppress_warning = self.config.get("show_unprivileged_warning") is False
        if self._smn_readable or suppress_warning:
            return
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        message.setWindowTitle("Limited telemetry without root")
        message.setText(
            "Some telemetry requires root privileges and is hidden for this session:\n\n"
            "• CPU Tctl/Tdie and CCD temperatures\n"
            "• IOD temperatures (all lanes)\n"
            "• PROCHOT CPU, PROCHOT EXT and HTC status\n\n"
            "These readings use direct SMN access. The SMN interface requires the "
            "register address to be written before it can be read, and that write "
            "operation is restricted to root. PM-table telemetry remains available.\n\n"
            "Start GNR Master with sudo to display the hidden sensors."
        )
        suppress_warning = QCheckBox("Don't show this warning again")
        suppress_warning.setChecked(False)
        message.setCheckBox(suppress_warning)
        message.setStandardButtons(QMessageBox.StandardButton.Ok)
        message.exec()
        self._save_config({"suppress_unprivileged_warning": suppress_warning.isChecked()})

    def _add_summary_section(self, layout, title):
        label = QLabel(title)
        label.setStyleSheet(
            f"background: transparent; color: {TEXT_MUTED}; font-size: 9px; font-weight: 800;"
        )
        layout.addWidget(label)

    def _add_summary_separator(self, layout):
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setStyleSheet(f"background: {BORDER}; border: none;")
        separator.setFixedWidth(1)
        layout.addWidget(separator)
        return separator

    def _add_summary_metric(self, layout, key, title, color, minimum_width=0):
        metric = QWidget()
        metric.setStyleSheet("background: transparent;")
        if minimum_width:
            metric.setMinimumWidth(minimum_width)
        metric_layout = QHBoxLayout(metric)
        metric_layout.setContentsMargins(0, 0, 0, 0)
        metric_layout.setSpacing(5)
        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"background: transparent; color: {TEXT_MUTED}; font-size: 12px; font-weight: 700;"
        )
        value_label = QLabel("--")
        value_label.setStyleSheet(
            f"background: transparent; color: {color}; font-size: 12px; font-weight: 700;"
        )
        if title:
            metric_layout.addWidget(title_label)
        metric_layout.addWidget(value_label)
        layout.addWidget(metric)
        self.summary_values[key] = value_label
        self.summary_widgets[key] = metric
        return metric

    def _set_summary(self, key, value):
        label = self.summary_values.get(key)
        if label is not None:
            label.setText(value)

    def _add_sensor_group(self, parent, label, expanded=True):
        item = QTreeWidgetItem([label, "", "", "", ""])
        item.setFirstColumnSpanned(True)
        parent_key = parent.data(0, Qt.ItemDataRole.UserRole) if parent is not None else ""
        group_key = f"{parent_key}/{label}" if parent_key else label
        expansion_config = self.config.get("sensor_group_expansion", {})
        if not isinstance(expansion_config, dict):
            expansion_config = {}
        saved_state = expansion_config.get(group_key)
        item.setData(0, Qt.ItemDataRole.UserRole, group_key)
        item.setForeground(0, QColor("#f8fafc"))
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        if parent is None:
            self.sensor_tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        # QTreeWidget only applies the initial expansion state once the item is
        # attached to the tree.  This keeps a fresh configuration fully open.
        item.setExpanded(saved_state if isinstance(saved_state, bool) else expanded)
        self.sensor_groups[group_key] = item
        return item

    def _add_sensor(self, parent, key, label, unit, tooltip="", current_color=None):
        item = QTreeWidgetItem([label, "--", "--", "--", "--"])
        for column in range(1, 5):
            item.setTextAlignment(
                column,
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            )
        item.setBackground(1, QColor(BG_TREE_CURRENT))
        item.setForeground(1, QColor(current_color or "#d6e6ff"))
        if tooltip:
            item.setToolTip(0, tooltip)
        parent.addChild(item)
        self.sensor_items[key] = item
        self.sensor_units[key] = unit
        return item

    def _build_sensor_tree(self):
        # Put the actual CPU identity in the table itself.  The decoder profile
        # remains an internal implementation detail and is not used as the
        # visible hardware name.
        self._add_sensor_group(None, self.cpu_name, expanded=True)

        temperatures = self._add_sensor_group(None, "Temperatures")
        tctl_item = self._add_sensor(
            temperatures, "tctl", "CPU (Tctl/Tdie)", "°C",
            "Direct Tctl/Tdie sensor via the profile-approved SMN register.",
        )
        tctl_item.setHidden(not self._smn_readable)
        for ccd in self.visible_ccds:
            ccd_item = self._add_sensor(
                temperatures, f"tccd{ccd}", self._ccd_label(ccd, "CPU "), "°C",
                "Direct CCD sensor via the profile-approved SMN register.",
            )
            ccd_item.setHidden(not self._smn_readable)
        core_temps = self._add_sensor_group(temperatures, "Cores")
        for core in self.visible_core_slots:
            self._add_sensor(core_temps, f"core_temp_{core}", self._core_label(core), "°C")

        l3 = self._add_sensor_group(temperatures, "L3 Cache")
        l3.setToolTip(0, "Per-CCD L3 cache temperature telemetry.")
        if self.profile and self.profile.ccd_l3_temperature is not None:
            for ccd in self.visible_ccds:
                self._add_sensor(l3, f"ccd_l3_temp_{ccd}",
                                 self._ccd_label(ccd, suffix=" L3 Cache"),
                                 "°C",
                                 "Per-CCD L3 cache temperature, validated for this profile.")
        else:
            l3.setText(0, "Not mapped for this profile")

        iod = self._add_sensor_group(temperatures, "IOD")
        iod.setToolTip(0, "Direct I/O-die temperature telemetry for this profile.")
        iod.setHidden(not self._smn_readable)
        if self.profile and self.profile.iod_smn_temp_addresses:
            lane_numbers = getattr(self.profile, "iod_smn_lane_numbers", ())
            for lane, address in enumerate(self.profile.iod_smn_temp_addresses):
                lane_number = (lane_numbers[lane]
                               if lane < len(lane_numbers) else lane + 1)
                self._add_sensor(
                    iod, f"iod_lane_{lane}",
                    f"IOD Lane {lane_number} (SMN 0x{address:05X})", "°C",
                    "Individual profile-approved I/O-die temperature lane.",
                )
        else:
            iod.setText(0, "Not mapped for this profile")

        limits_root = self._add_sensor_group(None, "CPU Limits")
        power_limits = self._add_sensor_group(limits_root, "Package Power & Current")
        self._add_sensor(power_limits, "ppt", "CPU PPT", "W")
        self._add_sensor(power_limits, "tdc", "CPU TDC", "A")
        if self.profile and self.profile.edc_value is not None:
            self._add_sensor(power_limits, "edc", "CPU EDC", "A",
                             f"Live PM-table candidate d[{self.profile.edc_value}], not yet SMU-confirmed")

        self._add_sensor(power_limits, "ppt_limit", "CPU PPT Limit", "W")
        self._add_sensor(power_limits, "tdc_limit", "CPU TDC Limit", "A")
        self._add_sensor(power_limits, "edc_limit", "CPU EDC Limit", "A")

        temperature_limits = self._add_sensor_group(limits_root, "Thermal")
        prochot_cpu = self._add_sensor(temperature_limits, "prochot_cpu", "Thermal Throttling (PROCHOT CPU)", "Yes/No",
                         "Read-only status from the profile-approved thermal status register.")
        prochot_ext = self._add_sensor(temperature_limits, "prochot_ext", "Thermal Throttling (PROCHOT EXT)", "Yes/No",
                         "Read-only status from the profile-approved thermal status register.")
        htc = self._add_sensor(temperature_limits, "htc", "Thermal Throttling (HTC)", "Yes/No",
                         "Read-only status from the profile-approved thermal status register.")
        for item in (prochot_cpu, prochot_ext, htc):
            item.setHidden(not self._smn_readable)
        self._add_sensor(temperature_limits, "thermal_limit", "Thermal Limit", "°C")

        power = self._add_sensor_group(None, "Power")
        for key, label in (
            ("socket_power", "CPU Socket Power"), ("cpu_power", "CPU Core Power"),
            ("core_power", "Core Power Sum"), ("soc_power", "CPU SoC Power"),
            ("vddio_power", "VDDIO MEM Power"), ("vdd18_power", "VDD18 Power"),
        ):
            self._add_sensor(power, key, label, "W")
        core_power = self._add_sensor_group(power, "Core Powers")
        for core in self.visible_core_slots:
            self._add_sensor(core_power, f"core_power_{core}", self._core_label(core), "W")

        clocks = self._add_sensor_group(None, "Clocks")
        for key, label in (("fclk", "Infinity Fabric Clock (FCLK)"),
                           ("uclk", "Memory Controller Clock (UCLK)"),
                           ("mclk", "Memory Clock (MCLK)")):
            self._add_sensor(clocks, key, label, "MHz")
        core_clocks = self._add_sensor_group(clocks, "Core Clocks", expanded=True)
        for core in self.visible_core_slots:
            self._add_sensor(core_clocks, f"core_clock_{core}", self._core_label(core), "MHz",
                             "Direct PM-table clock lane used by Ryzen Master")

        voltages = self._add_sensor_group(None, "Voltages")
        for key, label in (("vcore_peak", "Vcore Peak"), ("vcore_avg", "Vcore Average"),
                           ("vsoc", "VDDCR_SOC"), ("vdd_misc", "VDD_MISC"),
                           ("vddg_iod", "CLDO_VDDG_IOD"), ("vddg_ccd", "CLDO_VDDG_CCD"),
                           ("vddp", "CLDO_VDDP"), ("vid", "CPU VID"),
                           ("vid_limit", "VID Limit")):
            self._add_sensor(voltages, key, label, "V")
        core_voltages = self._add_sensor_group(voltages, "Core Voltages")
        for core in self.visible_core_slots:
            self._add_sensor(core_voltages, f"core_voltage_{core}", self._core_label(core), "V")

        residency_label = ("Core residency & FIT" if self.profile is None
                           or self.profile.core_fit is not None else "Core residency")
        residency = self._add_sensor_group(None, residency_label)
        ccd_summary = self._add_sensor_group(residency, "CCD residency summary")
        has_direct_c0 = self.profile is not None and self.profile.core_c0 is not None
        for ccd in self.visible_ccds:
            label = (self._ccd_label(ccd, suffix=" C0 residency") if has_direct_c0
                     else self._ccd_label(ccd, suffix=" active/load estimate"))
            self._add_sensor(ccd_summary, f"ccd_c0_{ccd}", label, "%",
                             "C0 is direct on the 9950X3D; 9800X3D uses 100 - CC6.",
                             ACCENT_GREEN)
            if has_direct_c0:
                self._add_sensor(ccd_summary, f"ccd_cc1_{ccd}",
                                 self._ccd_label(ccd, suffix=" CC1 residency"), "%",
                                 current_color=ACCENT_CYAN)
            self._add_sensor(ccd_summary, f"ccd_cc6_{ccd}",
                             self._ccd_label(ccd, suffix=" CC6 residency"), "%",
                             current_color=ACCENT_PURPLE)

        if self.profile is None or self.profile.core_fit is not None:
            fit_group = self._add_sensor_group(residency, "FIT / related metric")
            for core in self.visible_core_slots:
                self._add_sensor(fit_group, f"core_fit_{core}", self._core_label(core), "",
                                 "Per-core PM-table FIT/related metric; not a direct temperature or voltage reading.",
                                 ACCENT_ORANGE)

        c0_group = self._add_sensor_group(
            residency, "C0 residency · active cores" if has_direct_c0 else "Active/load estimate · 100 - CC6"
        )
        for core in self.visible_core_slots:
            self._add_sensor(c0_group, f"core_c0_{core}", self._core_label(core), "%",
                             current_color=ACCENT_GREEN)

        cc1_group = None
        if has_direct_c0:
            cc1_group = self._add_sensor_group(residency, "CC1 residency · light idle")
            for core in self.visible_core_slots:
                self._add_sensor(cc1_group, f"core_cc1_{core}", self._core_label(core), "%",
                                 current_color=ACCENT_CYAN)

        cc6_group = self._add_sensor_group(residency, "CC6 residency · deep idle")
        for core in self.visible_core_slots:
            self._add_sensor(cc6_group, f"core_cc6_{core}", self._core_label(core), "%",
                             current_color=ACCENT_PURPLE)

    def _format_sensor_value(self, value, unit):
        if value is None:
            return "--"
        if unit == "MHz":
            return f"{value:,.0f} MHz"
        if unit == "Yes/No":
            return "Yes" if value else "No"
        if unit in ("°C", "%"):
            return f"{value:.1f} {unit}"
        if unit in ("W", "A", "V"):
            return f"{value:.3f} {unit}"
        if unit == "int":
            return f"{value:.0f}"
        return f"{value:.2f}"

    def _set_sensor(self, key, value):
        item = self.sensor_items.get(key)
        if item is None:
            return
        unit = self.sensor_units[key]
        item.setText(1, self._format_sensor_value(value, unit))
        if unit == "Yes/No":
            # A live or historical throttling event is an alert condition, not
            # ordinary telemetry.  Colour every cell that renders as "Yes" so
            # a latched maximum/average is visible even after the live state
            # has returned to "No".
            item.setForeground(1, QColor(ACCENT_RED if value else "#d6e6ff"))
        if value is None:
            return
        stat = self.sensor_stats.get(key)
        if stat is None:
            stat = [value, value, value, 1]
            self.sensor_stats[key] = stat
        else:
            stat[0] = min(stat[0], value)
            stat[1] = max(stat[1], value)
            stat[2] += value
            stat[3] += 1
        item.setText(2, self._format_sensor_value(stat[0], unit))
        item.setText(3, self._format_sensor_value(stat[1], unit))
        item.setText(4, self._format_sensor_value(stat[2] / stat[3], unit))
        if unit == "Yes/No":
            for column, status in (
                (2, stat[0]),
                (3, stat[1]),
                (4, stat[2] / stat[3]),
            ):
                item.setForeground(
                    column, QColor(ACCENT_RED if status else "#d6e6ff")
                )

    def _reset_sensor_stats(self):
        self.sensor_stats.clear()
        for item in self.sensor_items.values():
            for column in range(2, 5):
                item.setText(column, "--")
        self.log_msg("Sensor minimum, maximum and average reset.", "STATUS", ACCENT_GREEN)

    def _build_logs_page(self):
        page, layout = self._page(
            "Kernel & Application Logs", "Filtered ryzen_smu / gnr_smu messages and application events"
        )
        panel = TelemetryPanel("LIVE LOG")
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet(
            f"background: {BG_INNER}; border: none; color: #a5b4c8; "
            "font-family: Consolas, monospace; font-size: 11px; padding: 8px;"
        )
        panel.layout_box.addWidget(self.log_text)
        layout.addWidget(panel)
        return page

    def _nav_button_style(self):
        return (
            f"QPushButton {{ color: {TEXT_MUTED}; background: transparent; border: none; "
            "border-radius: 6px; text-align: left; padding: 9px 11px; font-size: 12px; }} "
            f"QPushButton:hover {{ background: {BG_INNER}; color: {TEXT_MAIN}; }} "
            f"QPushButton:checked {{ background: #2d251d; color: {ACCENT_ORANGE}; "
            f"font-weight: 700; border-left: 3px solid {ACCENT_ORANGE}; }}"
        )

    def _control_button_style(self):
        return (
            f"QPushButton {{ color: #cbd5e1; background: {BG_PANEL}; border: 1px solid {BORDER}; "
            "border-radius: 6px; text-align: left; padding: 8px; font-size: 11px; }} "
            f"QPushButton:hover {{ border-color: {ACCENT_ORANGE}; color: {TEXT_MAIN}; }}"
        )

    def _show_page(self, index):
        self.pages.setCurrentIndex(index)
        style = self._nav_button_style()
        for i, button in enumerate(self.nav_buttons):
            button.setStyleSheet(style)
            button.setChecked(i == index)

    def log_msg(self, msg, level="INFO", color="#3b82f6"):
        self.log_text.append(f"<span style='color:{color};'>[{level}]</span> {msg}")
        self.log_text.verticalScrollBar().setValue(
            self.log_text.verticalScrollBar().maximum()
        )

    def _load_config(self):
        try:
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            # ``co_offsets`` used to be a local write cache. There is no
            # validated readback path, so keeping it would misrepresent stale
            # data as the current hardware configuration.
            if "co_offsets" in data:
                data.pop("co_offsets")
                with open(CONFIG_PATH, "w") as f:
                    json.dump(data, f, indent=2, sort_keys=True)
            return data
        except Exception:
            return {}

    @staticmethod
    def _sanitize_update_interval(value):
        try:
            return max(100, min(10000, int(value)))
        except (TypeError, ValueError):
            return 500

    def _refresh_display_topology(self):
        """Select UI rows without ever treating an inactive slot as telemetry."""
        self._active_slot_set = frozenset(self.core_slots)
        if self.profile is None:
            physical_slots = ()
        else:
            physical_slots = tuple(range(self.profile.slot_count or self.profile.cores))
        self.visible_core_slots = (physical_slots if self.show_disabled_cores
                                   else self.core_slots)
        self.visible_ccds = tuple(sorted({slot // 8 for slot in self.visible_core_slots}))

    def _core_label(self, slot):
        label = f"Core {slot} (CCD{slot // 8 + 1}"
        return label + (")" if slot in self._active_slot_set else ", disabled)")

    def _ccd_label(self, ccd, prefix="", suffix=""):
        label = f"{prefix}CCD{ccd + 1}{suffix}"
        return label if ccd in self.active_ccds else f"{label} (disabled)"

    def _fit_window_size(self, width, height):
        width = max(self.minimumWidth(), int(width))
        height = max(self.minimumHeight(), int(height))
        return QSize(width, height)

    def _default_window_size(self):
        column_width = sum(
            self.sensor_tree.header().sectionSize(column) for column in range(5)
        )
        scrollbar_width = self.sensor_tree.verticalScrollBar().sizeHint().width()
        width = self.sidebar_width + column_width + scrollbar_width + 52
        return self._fit_window_size(width, 900)

    def _restore_window_size(self):
        saved_size = self.config.get("window_size")
        if (isinstance(saved_size, list) and len(saved_size) == 2
                and all(isinstance(value, (int, float)) for value in saved_size)):
            size = self._fit_window_size(saved_size[0], saved_size[1])
        else:
            size = self._default_window_size()
        self.resize(size)

    def _save_config(self, updates):
        try:
            self.config.update(updates)
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w") as f:
                json.dump(self.config, f, indent=2, sort_keys=True)
            return True
        except Exception:
            return False

    def _queue_sensor_preferences(self, *_):
        self._preferences_timer.start(300)

    def _save_sensor_preferences(self):
        if not hasattr(self, "sensor_tree"):
            return
        header_state = bytes(self.sensor_tree.header().saveState().toBase64()).decode("ascii")
        group_expansion = {
            key: item.isExpanded() for key, item in self.sensor_groups.items()
        }
        self._save_config({
            "sensor_header_state": header_state,
            "sensor_group_expansion": group_expansion,
            "sensor_update_ms": self.sensor_update_ms,
            "window_size": [self.width(), self.height()],
        })

    def _set_update_interval(self, value):
        self.sensor_update_ms = self._sanitize_update_interval(value)
        if hasattr(self, "timer"):
            self.timer.setInterval(self.sensor_update_ms)
        self._queue_sensor_preferences()

    def _rebuild_overview_page(self):
        """Recreate the dashboard so a display-topology preference applies now."""
        current_index = self.pages.currentIndex()
        old_page = self.pages.widget(0)
        self.pages.removeWidget(old_page)
        old_page.deleteLater()
        self.pages.insertWidget(0, self._build_overview_page())
        self.pages.setCurrentIndex(current_index)

    def open_settings(self):
        dialog = SettingsDialog(
            self.sensor_update_ms, self.show_disabled_cores, self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        refresh_ms = self._sanitize_update_interval(dialog.refresh_input.value())
        show_disabled_cores = dialog.show_disabled_cores.isChecked()
        topology_changed = show_disabled_cores != self.show_disabled_cores
        self.show_disabled_cores = show_disabled_cores
        self._refresh_display_topology()
        self._set_update_interval(refresh_ms)
        self._save_config({
            "sensor_update_ms": self.sensor_update_ms,
            "show_disabled_cores": self.show_disabled_cores,
        })
        if topology_changed:
            self._rebuild_overview_page()
        self.log_msg("Settings saved.", "STATUS", ACCENT_GREEN)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_preferences_timer") and hasattr(self, "sensor_tree"):
            self._queue_sensor_preferences()

    def closeEvent(self, event):
        self._save_sensor_preferences()
        super().closeEvent(event)

    def send_smu_cmd(self, msg_id, arg0=0):
        permission_reason = smu_write_permission_reason()
        if permission_reason:
            self._show_smu_error(permission_reason)
            return False
        ok, why = smu_writes_supported()
        if not ok and not self._unvalidated_smu_override:
            self._show_smu_error(f"SMU writes disabled — {why}")
            return False
        if not smu_message_supported(self.profile, msg_id):
            self._show_smu_error(
                f"MSG {hex(msg_id)} is not in the {self.profile.name} command allowlist"
            )
            return False
        blocked, reason = msg_id_blocked(msg_id)
        if blocked:
            self._show_smu_error(reason)
            return False

        try:
            # 1) Write args: 6 x uint32 LE (arg0 in slot 0, rest = 0)
            args_bin = struct.pack("<6I", arg0 & 0xFFFFFFFF, 0, 0, 0, 0, 0)
            with open(SMU_ARGS_PATH, "wb") as f:
                f.write(args_bin)

            # 2) Write MSG ID as uint32 LE to trigger the command
            cmd_bin = struct.pack("<I", msg_id)
            with open(SMU_CMD_PATH, "wb") as f:
                f.write(cmd_bin)

            # 3) Read response
            with open(SMU_CMD_PATH, "rb") as f:
                rsp_data = f.read(4)
            rsp = struct.unpack("<I", rsp_data)[0] if len(rsp_data) == 4 else 0xFF

            rsp_str = {
                1: "OK",
                0xFD: "REJECTED",
                0xFE: "UNKNOWN_CMD",
                0xFF: "FAILED",
            }.get(rsp, f"0x{rsp:02X}")
            color = ACCENT_GREEN if rsp == 1 else ACCENT_RED
            self.log_msg(
                f"SMU MP1 -> MSG: {hex(msg_id)}, ARG0: {hex(arg0)} => RSP: {rsp_str}",
                "SMU",
                color,
            )
            if rsp != 1:
                self._show_smu_error(
                    f"SMU rejected the command (MSG {hex(msg_id)}, response {rsp_str})."
                )
            return rsp == 1
        except Exception as e:
            self._show_smu_error(f"SMU write failed: {str(e)}")
            return False

    def _show_smu_error(self, message):
        self.log_msg(f"SMU: {message}", "ERROR", ACCENT_RED)
        QMessageBox.critical(self, "SMU control failed", message)

    def _smu_controls_available(self):
        permission_reason = smu_write_permission_reason()
        if permission_reason:
            self._show_smu_error(permission_reason)
            return False
        ok, why = smu_writes_supported()
        if ok or self._unvalidated_smu_override:
            return True
        return self._confirm_unvalidated_smu_controls(why)

    def _confirm_unvalidated_smu_controls(self, reason):
        """Ask once per session before using a format-only control layout."""
        candidate = unvalidated_smu_control_profile(self.profile)
        if candidate is None:
            self._show_smu_error(f"SMU controls disabled — {reason}")
            return False

        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        message.setWindowTitle("SMU control warning")
        message.setText(
            "SMU control writes are not validated for this CPU."
        )
        message.setInformativeText(
            "Power-limit, thermal-limit, and Curve Optimizer changes may behave "
            "unexpectedly and can make the system unstable.\n\n"
            "Use at your own risk. Continue anyway?"
        )
        message.setMinimumWidth(500)
        cancel_button = message.addButton(
            "Cancel", QMessageBox.ButtonRole.RejectRole)
        continue_button = message.addButton(
            "Continue", QMessageBox.ButtonRole.AcceptRole)
        message.setDefaultButton(cancel_button)
        message.setEscapeButton(cancel_button)
        message.exec()
        if message.clickedButton() is not continue_button:
            self.log_msg("Unvalidated SMU control declined by user.", "STATUS")
            return False

        self.profile = candidate
        self._unvalidated_smu_override = True
        self.log_msg(
            "UNVALIDATED SMU CONTROL ENABLED for this session by user confirmation.",
            "WARNING", ACCENT_ORANGE,
        )
        return True

    def open_power_control(self):
        if not self._smu_controls_available():
            return
        dlg = PowerControlDialog(
            self.current_ppt, self.current_tdc, self.current_edc,
            self.current_thermal, self
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            requested = {
                "PPT": dlg.inputs["PPT"].value(),
                "TDC": dlg.inputs["TDC"].value(),
                "EDC": dlg.inputs["EDC"].value(),
                "Thermal Limit": dlg.inputs["Thermal Limit"].value(),
            }
            current = {"PPT": self.current_ppt, "TDC": self.current_tdc,
                       "EDC": self.current_edc,
                       "Thermal Limit": self.current_thermal}
            changed = {name: value for name, value in requested.items()
                       if abs(value - current[name]) >= 0.001}
            if not changed:
                self.log_msg("Power limits unchanged; nothing sent.", "STATUS")
                return
            commands = {
                "PPT": self.profile.ppt_msg,
                "TDC": self.profile.tdc_msg,
                "EDC": self.profile.edc_msg,
                "Thermal Limit": self.profile.thermal_msg,
            }
            for name, value in changed.items():
                # PPT/TDC/EDC use milli-units; SetTctlMax takes whole °C.
                arg0 = int(value if name == "Thermal Limit" else value * 1000)
                if self.send_smu_cmd(commands[name], arg0):
                    attr = {
                        "PPT": "current_ppt",
                        "TDC": "current_tdc",
                        "EDC": "current_edc",
                        "Thermal Limit": "current_thermal",
                    }[name]
                    setattr(self, attr, value)
                else:
                    break

    def open_core_control(self):
        if not self._smu_controls_available():
            return
        dlg = CoreControlDialog(self.current_co, self.core_slots, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            changed = [(core, spin.value()) for core, spin in dlg.spins.items()
                       if spin.value() != self.current_co[core]]
            if not changed:
                self.log_msg("Curve Optimizer unchanged; nothing sent.", "STATUS")
                return
            applied = []
            for core, value in changed:
                msg_id, arg0 = curve_optimizer_command(self.profile, core, value)
                if self.send_smu_cmd(msg_id, arg0):
                    self.current_co[core] = value
                    applied.append(core)
                else:
                    break
            if applied:
                self.log_msg(
                    f"CO offsets applied for this session on cores {applied}: "
                    f"{self.current_co}", "STATUS", ACCENT_GREEN,
                )

    def _read_pm_limits(self):
        # Stock 9800X3D spec is the fallback: on unvalidated hardware these offsets
        # hold something else, and this feeds the write dialog's defaults.
        if not hardware_supported()[0]:
            return 162.0, 120.0, 180.0, 95.0
        try:
            with open("/sys/kernel/ryzen_smu_drv/pm_table", "rb") as f:
                d = struct.unpack(f"<{self.profile.float_count}f",
                                  f.read(self.profile.table_size))
            # PPT=d[2], TDC=d[8] (0x020), EDC=d[63] (0x0FC). Corrected 2026-07-30:
            # this used to return d[10] as TDC — that offset is the configured
            # configured thermal limit in °C, so the write dialog once pre-filled
            # TDC and EDC from the wrong offsets. Read d[10] from the active
            # profile instead of hardcoding a thermal value.
            return d[2], d[8], d[63], d[10]
        except Exception:
            return 162.0, 120.0, 180.0, 95.0

    def _core_frequency_mhz(self, table, core):
        """Use only an explicit, profile-approved PM-table frequency lane."""
        if self.profile.core_frequency is not None:
            return table[self.profile.core_frequency + core] * 1000
        return None

    def _temperature_sample_is_valid(self, d, tctl, ccd_temperatures):
        """Reject a whole snapshot if any displayed temperature is corrupt."""
        temperatures = [
            ("CPU (Tctl/Tdie)", tctl),
            ("PM Tctl/Tdie", d[11]),
            ("Thermal limit", d[10]),
        ]
        temperatures.extend(
            (f"CPU CCD{ccd + 1}", temperature)
            for ccd, temperature in zip(self.visible_ccds, ccd_temperatures)
        )
        temperatures.extend(
            (f"Core {core}", d[self.profile.core_temp + core])
            for core in self.visible_core_slots
        )
        if self.profile.ccd_count:
            temperatures.extend(
                (f"CCD L3 temperature {ccd + 1}",
                 d[self.profile.ccd_l3_temperature + ccd])
                for ccd in self.visible_ccds
            )

        for name, temperature in temperatures:
            if temperature is None:
                continue
            if (not math.isfinite(temperature)
                    or not TEMPERATURE_SAMPLE_MIN_C <= temperature <= TEMPERATURE_SAMPLE_MAX_C):
                if not self._temperature_sample_discarded:
                    self.log_msg(
                        f"Discarded telemetry sample: {name} reported "
                        f"{temperature!r} °C.",
                        "WARNING", ACCENT_ORANGE,
                    )
                self._temperature_sample_discarded = True
                return False
        self._temperature_sample_discarded = False
        return True

    def _update_sensor_tree(self, d):
        tctl = (read_profile_tctl_temperature(self.profile)
                if self._smn_readable else None)
        ccd_temperatures = [
            (read_profile_ccd_temperature(self.profile, ccd)
             if self._smn_readable else None)
            for ccd in self.visible_ccds
        ]
        if not self._temperature_sample_is_valid(d, tctl, ccd_temperatures):
            return False

        vcores = [d[self.profile.core_voltage + slot] for slot in self.core_slots]
        self._set_sensor("tctl", tctl)
        self._set_summary("cpu", f"{tctl:.1f} °C" if tctl is not None else "--")
        self._set_sensor("ppt", d[3])
        self._set_sensor("ppt_limit", d[2])
        self._set_sensor("tdc", d[9])
        self._set_sensor("tdc_limit", d[8])
        self._set_sensor("edc_limit", d[63])
        self._set_sensor("thermal_limit", d[10])
        self.current_thermal = d[10]
        if self._smn_readable:
            thermal = read_profile_prochot_status(self.profile)
            self._set_sensor("prochot_cpu", thermal["prochot_cpu"])
            self._set_sensor("prochot_ext", thermal["prochot_ext"])
            self._set_sensor("htc", thermal["htc"])
            iod_lanes = read_profile_iod_lanes(self.profile)
            for lane, value in enumerate(iod_lanes):
                key = f"iod_lane_{lane}"
                self._set_sensor(key, value)
                item = self.sensor_items.get(key)
                if item is not None:
                    item.setHidden(value is None)
        socket_power = d[26] if self.profile.pm_version == 0x620205 else d[20]
        self._set_sensor("socket_power", socket_power)
        if self.profile.edc_value is not None:
            current_edc = d[self.profile.edc_value]
            self._set_sensor("edc", current_edc)
            self._set_summary(
                "limits",
                f"PPT {d[3]:.0f}/{d[2]:.0f} W · TDC {d[9]:.0f}/{d[8]:.0f} A · "
                f"EDC {current_edc:.0f}/{d[63]:.0f} A",
            )
        else:
            self._set_summary(
                "limits",
                f"PPT {d[3]:.0f}/{d[2]:.0f} W · TDC {d[9]:.0f}/{d[8]:.0f} A · EDC {d[63]:.0f} A",
            )
        self._set_sensor("cpu_power", d[20])
        self._set_sensor("core_power", sum(d[self.profile.core_power + slot]
                                            for slot in self.core_slots))
        self._set_sensor("soc_power", d[21])
        self._set_sensor("vddio_power", d[22])
        self._set_sensor("vdd18_power", d[23])
        self._set_sensor("fclk", d[71])
        self._set_sensor("uclk", d[75])
        self._set_sensor("mclk", d[79])
        self._set_sensor("vcore_peak", max(vcores))
        self._set_sensor("vcore_avg", sum(vcores) / self.core_count)
        self._set_sensor("vsoc", d[83])
        self._set_sensor("vdd_misc", d[58])
        self._set_sensor("vddg_iod", d[259])
        self._set_sensor("vddg_ccd", d[261])
        self._set_sensor("vddp", d[269])
        self._set_sensor("vid", d[19])
        self._set_sensor("vid_limit", d[18])

        for ccd, ccd_temp in zip(self.visible_ccds, ccd_temperatures):
            self._set_sensor(f"tccd{ccd}", ccd_temp)
            self._set_summary(
                f"ccd{ccd}", f"{ccd_temp:.1f} °C" if ccd_temp is not None else "--"
            )

        # Per-CCD residency comes from PM lanes.  When the user opted in to
        # disabled slots, expose their raw lane contents too. The direct CCD
        # read above is also attempted, but can be invalid when firmware
        # power-gates the CCD.
        for ccd in self.visible_ccds:
            ccd_slots = [slot for slot in self.visible_core_slots if slot // 8 == ccd]
            cc6_values = [d[self.profile.core_cc6 + slot] for slot in ccd_slots]
            if self.profile.core_c0 is not None:
                c0_values = [d[self.profile.core_c0 + slot] for slot in ccd_slots]
                cc1_values = [d[self.profile.core_cc1 + slot] for slot in ccd_slots]
                self._set_sensor(f"ccd_c0_{ccd}", sum(c0_values) / len(c0_values))
                self._set_sensor(f"ccd_cc1_{ccd}", sum(cc1_values) / len(cc1_values))
            else:
                self._set_sensor(f"ccd_c0_{ccd}", 100 - sum(cc6_values) / len(cc6_values))
            self._set_sensor(f"ccd_cc6_{ccd}", sum(cc6_values) / len(cc6_values))
        if self.profile.ccd_count:
            for ccd in self.visible_ccds:
                self._set_sensor(f"ccd_l3_temp_{ccd}", d[self.profile.ccd_l3_temperature + ccd])

        frequencies = []
        for core in self.visible_core_slots:
            freq = self._core_frequency_mhz(d, core)
            if core in self._active_slot_set and freq is not None:
                frequencies.append(freq)
            self._set_sensor(f"core_temp_{core}", d[self.profile.core_temp + core])
            self._set_sensor(f"core_clock_{core}", freq)
            self._set_sensor(f"core_power_{core}", d[self.profile.core_power + core])
            self._set_sensor(f"core_voltage_{core}", d[self.profile.core_voltage + core])
            if self.profile.core_fit is not None:
                self._set_sensor(f"core_fit_{core}", d[self.profile.core_fit + core])
            self._set_sensor(f"core_cc6_{core}", d[self.profile.core_cc6 + core])
            if self.profile.core_c0 is not None:
                self._set_sensor(f"core_c0_{core}", d[self.profile.core_c0 + core])
                self._set_sensor(f"core_cc1_{core}", d[self.profile.core_cc1 + core])
            else:
                self._set_sensor(f"core_c0_{core}", 100 - d[self.profile.core_cc6 + core])
        if frequencies:
            self._set_summary("frequency", f"{max(frequencies):.0f} MHz")
        else:
            self._set_summary("frequency", "--")
        return True

    def update_data(self):
        ok, why = hardware_supported()
        if not ok:
            # update_data runs on a timer; log the refusal once, not every tick.
            if not getattr(self, "_hw_warned", False):
                self._hw_warned = True
                self.log_msg(f"UNVALIDATED HARDWARE: {why}. Telemetry and SMU writes "
                             f"disabled — the offsets would be wrong, not missing.",
                             "ERROR", ACCENT_RED)
            return

        try:
            with open("/sys/kernel/ryzen_smu_drv/pm_table", "rb") as f:
                data = f.read(self.profile.table_size)
                if len(data) == self.profile.table_size:
                    d = struct.unpack(f"<{self.profile.float_count}f", data)
                    # Zone 0x000 is the Zen (LIMIT, VALUE) pair layout — corrected
                    # 2026-07-30. d[8] is TDC (not EDC), d[10] is the thermal limit
                    # in °C (not TDC in A), and EDC's limit lives at d[63].
                    if self._update_sensor_tree(d):
                        self.current_ppt = d[2]
                        self.current_edc = d[63]
                        self.current_tdc = d[8]
                        self.current_thermal = d[10]

        except FileNotFoundError:
            pass
        except Exception as e:
            pass


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = GNRMaster()
    w.show()
    sys.exit(app.exec())
