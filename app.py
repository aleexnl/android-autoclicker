"""Qt desktop workflow editor. Device I/O never runs on the GUI thread."""
from __future__ import annotations

import copy
import argparse
import json
import math
import os
import queue
import re
import sys
import threading
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QRubberBand, QScrollArea,
    QStyle, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from engine import ADB, AutomationError, load_workflow, run_workflow, save_workflow, validate
from workflow_model import DEFAULTS, FIELDS, FIELDS_BY_TYPE, new_step
from platform_style import PlatformStyle


def data_directory():
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "AndroidVisualAutoclicker"


ROOT = Path(__file__).resolve().parent
DATA = data_directory()
SETTINGS_PATH = DATA / "settings.json"
TEMPLATES = DATA / "templates"
APPEARANCES = ("System", "Light", "Dark")


def load_appearance():
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        value = settings.get("appearance") if isinstance(settings, dict) else None
        return value if value in APPEARANCES else "System"
    except (OSError, ValueError):
        return "System"


def save_appearance(value):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({"appearance": value}, indent=2), encoding="utf-8")
    temporary.replace(SETTINGS_PATH)


class CropCanvas(QLabel):
    selected = Signal(object)

    def __init__(self, image, parent=None):
        super().__init__(parent)
        self.image = image
        scale = min(1.0, 900 / image.width, 650 / image.height)
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        preview = image.convert("RGB").resize(size)
        qimage = QImage(preview.tobytes(), *size, size[0] * 3, QImage.Format.Format_RGB888).copy()
        self.setPixmap(QPixmap.fromImage(qimage))
        self.setFixedSize(*size)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.band = QRubberBand(QRubberBand.Shape.Rectangle, self)
        self.origin = None

    def bounded(self, point):
        return QPoint(max(0, min(self.width(), point.x())), max(0, min(self.height(), point.y())))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = self.bounded(event.position().toPoint())
            self.band.setGeometry(QRect(self.origin, self.origin))
            self.band.show()

    def mouseMoveEvent(self, event):
        if self.origin is not None:
            self.band.setGeometry(QRect(self.origin, self.bounded(event.position().toPoint())).normalized())

    def mouseReleaseEvent(self, event):
        if self.origin is None or event.button() != Qt.MouseButton.LeftButton:
            return
        end = self.bounded(event.position().toPoint())
        x1, x2 = sorted((self.origin.x(), end.x()))
        y1, y2 = sorted((self.origin.y(), end.y()))
        self.origin = None
        if x2 - x1 < 12 or y2 - y1 < 12:
            return
        box = (round(x1 * self.image.width / self.width()), round(y1 * self.image.height / self.height()),
               round(x2 * self.image.width / self.width()), round(y2 * self.image.height / self.height()))
        self.selected.emit(box)


class CropDialog(QDialog):
    def __init__(self, parent, image, save_to):
        super().__init__(parent)
        self.setWindowTitle("Capture template")
        self.image, self.save_to = image, save_to
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Drag a rectangle at least 12 pixels wide and tall around the feature."))
        self.canvas = CropCanvas(image)
        layout.addWidget(self.canvas)
        self.canvas.selected.connect(self.save_crop)

    def save_crop(self, box):
        try:
            self.save_to.parent.mkdir(parents=True, exist_ok=True)
            self.image.crop(box).save(self.save_to)
        except OSError as exc:
            QMessageBox.critical(self, "Could not save template", str(exc))
            return
        self.accept()


class Editor(QMainWindow):
    def __init__(self, *, startup=True):
        super().__init__()
        self.setWindowTitle("Android Visual Autoclicker — Workflow Studio")
        self.resize(1200, 850)
        self.setMinimumSize(850, 600)
        self.workflow_path = DATA / "workflow.json"
        self.steps = [new_step("find_image", set()), new_step("wait", {"step_1"}), new_step("stop", {"step_1", "step_2"})]
        self.running = False
        self.busy = False
        self.closing = False
        self.stop_event = threading.Event()
        self.messages = queue.Queue()
        self.workers = []
        self.hotkey_listener = None
        self.vars, self.rows = {}, {}
        self.editing_id = None
        self.form_dirty = False
        self._build()
        self.platform_style = PlatformStyle(self)
        self.change_appearance(persist=False)
        self.refresh_tree(self.steps[0]["id"])
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.process_messages)
        self.timer.start(30)
        if startup:
            self.start_hotkey()
            QTimer.singleShot(0, self.refresh_devices)

    @staticmethod
    def button(layout, label, callback):
        button = QPushButton(label)
        button.setMinimumHeight(32)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        main = QVBoxLayout(central)
        main.setContentsMargins(20, 16, 20, 16)
        main.setSpacing(12)
        # Keep the system palette and native focus/hover rendering on every OS.
        self.setStyleSheet("QTreeView::item { padding: 8px 4px; } "
                           "QLineEdit, QComboBox { min-height: 28px; } "
                           "QPushButton { padding: 4px 10px; } "
                           "QLabel[section=true] { font-weight: bold; }")
        header = QHBoxLayout()
        title = QLabel("Workflow Studio")
        font = title.font()
        font.setPointSize(21)
        font.setBold(True)
        title.setFont(font)
        heading = QVBoxLayout()
        heading.setSpacing(3)
        heading.addWidget(title)
        heading.addWidget(QLabel("Build, test, and run Android automations."))
        header.addLayout(heading)
        header.addStretch()
        header.addWidget(QLabel("Appearance"))
        self.appearance = QComboBox()
        self.appearance.setAccessibleName("Appearance")
        self.appearance.addItems(APPEARANCES)
        self.appearance.setCurrentText(load_appearance())
        self.appearance.currentTextChanged.connect(self.change_appearance)
        header.addWidget(self.appearance)
        main.addLayout(header)
        file_menu = self.menuBar().addMenu("&File")
        self.file_actions = []
        self.file_buttons = []
        for label, shortcut, callback in [("Open…", QKeySequence.StandardKey.Open, self.open_file),
                                           ("Save", QKeySequence.StandardKey.Save, self.save),
                                           ("Save as…", QKeySequence.StandardKey.SaveAs, self.save_as)]:
            action = QAction(label, self)
            action.setShortcut(shortcut)
            action.triggered.connect(callback)
            file_menu.addAction(action)
            self.file_actions.append(action)
            self.file_buttons.append(self.button(header, label, callback))
        self.device_panel = QWidget()
        toolbar = QHBoxLayout(self.device_panel)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.addWidget(QLabel("1  Connect device"))
        self.device = QComboBox()
        self.device.setEditable(True)
        self.device.setMinimumWidth(160)
        self.device.setAccessibleName("ADB device")
        self.device.lineEdit().setPlaceholderText("Select a device or enter its serial")
        toolbar.addWidget(self.device, 1)
        self.button(toolbar, "Refresh", self.refresh_devices)
        self.button(toolbar, "Connect MuMu…", self.connect_mumu)
        self.capture_btn = self.button(toolbar, "Capture template…", self.capture)
        main.addWidget(self.device_panel)
        controls = QHBoxLayout()
        self.hotkey_status = QLabel("F8 stops while the app is focused")
        self.hotkey_status.setWordWrap(True)
        self.run_state = QLabel("Ready")
        self.run_state.setProperty("section", True)
        controls.addWidget(self.run_state)
        controls.addWidget(self.hotkey_status, 1)
        self.start_btn = self.button(controls, "Run (F6)", self.run)
        self.stop_btn = self.button(controls, "STOP (F8)", self.stop)
        self.start_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.stop_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.stop_btn.setEnabled(False)
        main.addLayout(controls)
        for key, callback in [("F6", self.run), ("F8", self.stop), ("Escape", self.stop)]:
            action = QAction(self)
            action.setShortcut(QKeySequence(key))
            action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
            action.triggered.connect(callback)
            self.addAction(action)
        self.split = QSplitter()
        main.addWidget(self.split, 1)
        left = QWidget()
        left.setAutoFillBackground(True)
        layout = QVBoxLayout(left)
        layout.setContentsMargins(12, 12, 12, 12)
        self.steps_heading = QLabel("2  Workflow steps")
        self.steps_heading.setProperty("section", True)
        layout.addWidget(self.steps_heading)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Step ID", "Action", "Transitions"])
        self.tree.setAccessibleName("Workflow steps")
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemSelectionChanged.connect(self.on_select)
        layout.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self.kind = QComboBox()
        self.kind.setAccessibleName("Action to add")
        self.kind.addItems(DEFAULTS)
        row.addWidget(self.kind)
        self.button(row, "Add step", self.add_step)
        row.addStretch()
        layout.addLayout(row)
        row = QHBoxLayout()
        self.step_buttons = []
        for label, callback in [("Clone", self.clone_step), ("Delete", self.delete_step),
                                ("Move up", lambda: self.move(-1)), ("Move down", lambda: self.move(1))]:
            self.step_buttons.append(self.button(row, label, callback))
        layout.addLayout(row)
        row.addStretch()
        help_text = QLabel("Branches refer to step IDs. Blank next continues to the following row.")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.split.addWidget(left)
        inspector = QWidget()
        inspector_layout = QVBoxLayout(inspector)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        form = QWidget()
        form.setAutoFillBackground(True)
        self.form = QFormLayout(form)
        self.form.setContentsMargins(18, 16, 18, 16)
        self.form.setVerticalSpacing(8)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.editor_heading = QLabel("3  Step settings")
        self.editor_heading.setProperty("section", True)
        self.form.addRow(self.editor_heading)
        self.action_help = QLabel()
        self.action_help.setWordWrap(True)
        self.form.addRow(self.action_help)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for name, label in FIELDS:
            if name == "type":
                widget = QComboBox()
                widget.addItems(DEFAULTS)
                widget.currentTextChanged.connect(self.change_type)
            elif name == "click_match":
                widget = QCheckBox()
            else:
                widget = QLineEdit()
            widget.setAccessibleName(label)
            self.vars[name] = widget
            wrap = QWidget()
            row = QHBoxLayout(wrap)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(widget, 1)
            if name == "image":
                self.button(row, "Browse…", self.browse_image)
            if name == "region":
                self.button(row, "Clear", lambda: self.vars["region"].clear())
            if name in {"on_true", "on_false", "next"}:
                self.button(row, "IDs", self.show_ids)
            field_label = QLabel(label)
            field_label.setBuddy(widget)
            self.form.addRow(field_label, wrap)
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self.mark_form_dirty)
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self.mark_form_dirty)
            else:
                widget.currentTextChanged.connect(self.mark_form_dirty)
            self.rows[name] = wrap
        hints = {"image": "Choose a captured PNG template", "region": "Blank = entire screen",
                 "next": "Blank = next row", "on_true": "Blank = next row", "on_false": "Blank = next row",
                 "key": "e.g. KEYCODE_BACK"}
        for name, hint in hints.items():
            self.vars[name].setPlaceholderText(hint)
            self.vars[name].setToolTip(hint)
        self.draft_status = QLabel("Changes applied")
        self.draft_status.setWordWrap(True)

        actions = QHBoxLayout()
        self.apply_button = self.button(actions, "Apply changes", self.apply)
        self.revert_button = self.button(actions, "Revert", self.revert_form)

        scroll.setWidget(form)
        inspector_layout.addWidget(scroll, 1)
        inspector_layout.addWidget(self.draft_status)
        inspector_layout.addLayout(actions)
        self.split.addWidget(inspector)
        self.material_surfaces = [left, form]
        self.split.setSizes([600, 500])
        log_header = QHBoxLayout()
        self.log_toggle = QPushButton("Execution log")
        self.log_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.log_toggle.setCheckable(True)
        self.log_toggle.setChecked(True)
        self.log_toggle.setToolTip("Show or hide execution details")
        log_header.addWidget(self.log_toggle)
        log_header.addStretch()
        self.button(log_header, "Clear log", lambda: self.log_view.clear())
        main.addLayout(log_header)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setAccessibleName("Execution log")
        self.log_view.setPlaceholderText("Run a workflow to see progress and device messages here.")
        self.log_view.setMaximumHeight(90)
        self.log_toggle.toggled.connect(self.log_view.setVisible)
        main.addWidget(self.log_view)
        self.statusBar().showMessage("Connect an Android device or start an emulator, then press Refresh.")

    def change_appearance(self, _value=None, *, persist=True):
        value = self.appearance.currentText()
        scheme = {"System": Qt.ColorScheme.Unknown, "Light": Qt.ColorScheme.Light, "Dark": Qt.ColorScheme.Dark}[value]
        QApplication.styleHints().setColorScheme(scheme)
        self.platform_style.set_appearance(value)
        if persist:
            try:
                save_appearance(value)
            except OSError as exc:
                self.logging(f"Could not save appearance: {exc}")

    def logging(self, message):
        self.messages.put(("log", str(message)))

    def start_hotkey(self):
        # Import lazily: pynput can fail on Linux without X or on restricted hosts.
        if sys.platform.startswith("linux") and os.environ.get("WAYLAND_DISPLAY"):
            self.hotkey_status.setText("Wayland: use STOP or F8 with this app focused")
            return
        try:
            from pynput import keyboard
            def pressed(key):
                if key == keyboard.Key.f8:
                    self.stop_event.set()
            self.hotkey_listener = keyboard.Listener(on_press=pressed)
            if not getattr(self.hotkey_listener, "IS_TRUSTED", True):
                self.hotkey_status.setText("Global F8 needs OS permission; focused F8 and STOP remain available")
                self.hotkey_listener = None
                return
            self.hotkey_listener.daemon = True
            self.hotkey_listener.start()
            self.hotkey_status.setText("Global F8 emergency stop enabled")
        except Exception as exc:
            self.hotkey_status.setText("Global F8 unavailable; use STOP or focused F8")
            self.logging(f"Global hotkey unavailable: {exc}")

    def background(self, label, work, success):
        if self.busy or self.running or self.closing:
            return
        self.busy = True
        self.set_controls()
        self.statusBar().showMessage(label)
        def worker():
            try:
                result = work()
                self.messages.put(("result", (success, result, None)))
            except Exception as exc:
                self.messages.put(("result", (success, None, str(exc))))
        self.launch(worker)

    def launch(self, worker):
        thread = threading.Thread(target=worker, daemon=True)
        self.workers.append(thread)
        thread.start()

    def set_controls(self):
        available = not (self.running or self.busy or self.closing)
        self.run_state.setText("Closing…" if self.closing else "Running" if self.running else "Working…" if self.busy else "Ready")
        self.device_panel.setEnabled(available)
        self.start_btn.setEnabled(available)
        self.stop_btn.setEnabled(self.running and not self.closing)
        self.split.setEnabled(available)
        for action in self.file_actions:
            action.setEnabled(available)
        for button in self.file_buttons:
            button.setEnabled(available)

    def process_messages(self):
        for _ in range(200):
            try:
                event, value = self.messages.get_nowait()
            except queue.Empty:
                break
            if event == "log":
                self.log_view.appendPlainText(value)
            elif event == "step":
                for i in range(self.tree.topLevelItemCount()):
                    item = self.tree.topLevelItem(i)
                    if item.text(0) == value:
                        self.tree.setCurrentItem(item)
                        self.tree.scrollToItem(item)
                self.statusBar().showMessage(f"Running {value} — F8 to stop")
            elif event == "done":
                self.running = False
                self.set_controls()
                self.statusBar().showMessage(value)
                self.run_state.setText(value)
                if value.startswith("Failed"):
                    self.log_toggle.setChecked(True)
            elif event == "result":
                callback, result, error = value
                self.busy = False
                self.set_controls()
                if not self.closing:
                    self.statusBar().showMessage("Ready" if error is None else "Device operation failed")
                    if error:
                        self.logging(error)
                        QMessageBox.warning(self, "Device operation failed", error)
                    else:
                        callback(result)
        self.workers = [thread for thread in self.workers if thread.is_alive()]
        if self.closing and not self.workers:
            self.close()

    def refresh_devices(self):
        self.background("Looking for devices…", lambda: ADB().devices(), self.devices_found)

    def devices_found(self, devices, preferred=None):
        selected = preferred or self.device.currentText()
        self.device.clear()
        self.device.addItems(devices)
        if selected in devices:
            self.device.setCurrentText(selected)
        self.logging("Devices: " + (", ".join(devices) or "none connected"))
        if not devices:
            self.statusBar().showMessage("No devices found. Enable USB debugging and authorize your computer, or connect MuMu.")

    def connect_mumu(self):
        if self.busy or self.running:
            return
        port, ok = QInputDialog.getInt(self, "Connect MuMu", "ADB port from MuMu Issues Diagnostic:", 16384, 1, 65535)
        if not ok:
            return
        serial = f"127.0.0.1:{port}"
        def connect():
            adb = ADB()
            result = adb.command("connect", serial)
            return result, adb.devices()
        def connected(result):
            message, devices = result
            self.logging(message.strip())
            self.devices_found(devices, serial)
            if serial not in devices:
                QMessageBox.warning(self, "Connection not ready", f"{serial} is not connected. Check the emulator's ADB port.")
        self.background("Connecting to emulator…", connect, connected)

    def current_adb(self):
        serial = self.device.currentText().strip()
        if not serial:
            raise AutomationError("Choose a connected ADB device first")
        return ADB(serial=serial)

    def capture(self):
        if self.busy or self.running:
            return
        try:
            adb = self.current_adb()
        except AutomationError as exc:
            QMessageBox.warning(self, "No device", str(exc))
            return
        self.background("Capturing device screen…", adb.screenshot, self.captured)

    def captured(self, image):
        target, _ = QFileDialog.getSaveFileName(self, "Save template", str(TEMPLATES / "target.png"), "PNG (*.png)")
        if target and CropDialog(self, image, Path(target)).exec() == QDialog.DialogCode.Accepted:
            self.vars["image"].setText(self.relative_image(Path(target)))

    def refresh_tree(self, selected=None):
        self.steps_heading.setText(f"2  Workflow steps · {len(self.steps)}")
        self.tree.blockSignals(True)
        self.tree.clear()
        for step in self.steps:
            flow = [f"{tag}: {step[field]}" for field, tag in [("on_true", "Found"), ("on_false", "Missing"), ("next", "Next")] if step.get(field)]
            item = QTreeWidgetItem([step["id"], step["type"], "  ".join(flow) or ("End workflow" if step["type"] == "stop" else "Continue to next row")])
            self.tree.addTopLevelItem(item)
            if step["id"] == selected:
                self.tree.setCurrentItem(item)
        self.tree.blockSignals(False)
        self.on_select()

    def get_selected(self):
        item = self.tree.currentItem()
        return next((step for step in self.steps if item and step["id"] == item.text(0)), None)

    def set_value(self, name, value):
        widget = self.vars[name]
        widget.blockSignals(True)
        if isinstance(widget, QComboBox):
            widget.setCurrentText(str(value))
        elif isinstance(widget, QCheckBox):
            widget.setChecked(bool(value))
        else:
            widget.setText(",".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value))
        widget.blockSignals(False)

    def mark_form_dirty(self, *_):
        self.form_dirty = True
        self.draft_status.setText("Pending edits · switch steps to apply, or use Apply changes.")
        self.apply_button.setEnabled(True)
        self.revert_button.setEnabled(True)

    def revert_form(self):
        self.form_dirty = False
        self.on_select()

    def select_id(self, identifier):
        self.tree.blockSignals(True)
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if item.text(0) == identifier:
                self.tree.setCurrentItem(item)
                break
        self.tree.blockSignals(False)

    def on_select(self):
        step = self.get_selected()
        if self.form_dirty and step and self.editing_id and step["id"] != self.editing_id:
            target = step["id"]
            self.select_id(self.editing_id)
            if not self.apply():
                return
            self.select_id(target)
            step = self.get_selected()
        self.editing_id = step["id"] if step else None
        self.form_dirty = False
        for name in self.vars:
            self.set_value(name, step.get(name, "") if step else "")
        self.show_fields(step["type"] if step else "stop")
        self.editor_heading.setText(f"3  Step settings · {step['id']}" if step else "3  Add a step to begin")
        self.draft_status.setText("Changes applied" if step else "Choose an action on the left and select Add step.")
        self.apply_button.setEnabled(False)
        self.revert_button.setEnabled(False)
        for widget in self.rows.values():
            widget.setEnabled(step is not None)
        index = self.steps.index(step) if step else -1
        for button, enabled in zip(self.step_buttons, [bool(step), bool(step), index > 0, 0 <= index < len(self.steps) - 1]):
            button.setEnabled(enabled)

    def show_fields(self, kind):
        descriptions = {
            "find_image": "Find a template on the device screen, then tap it or branch based on the result.",
            "tap": "Tap a position in device pixels, measured from the top-left corner.",
            "swipe": "Drag from the start coordinates to the end coordinates over the given duration.",
            "wait": "Pause before continuing to the next step.",
            "key": "Send an Android key such as KEYCODE_BACK or KEYCODE_HOME.",
            "text": "Type text into the focused field on the device.",
            "goto": "Jump to a step ID. Use a stop step to end a loop.",
            "stop": "End the workflow. No further steps will run.",
        }
        self.action_help.setText(descriptions.get(kind, ""))
        visible = {"id", "type", *FIELDS_BY_TYPE.get(kind, [])}
        for name, row in self.rows.items():
            self.form.setRowVisible(row, name in visible)

    def change_type(self, kind):
        for name, value in DEFAULTS[kind].items():
            self.set_value(name, value)
        self.show_fields(kind)

    def apply(self):
        if self.running or self.busy:
            return False
        step = self.get_selected()
        if step is None:
            return False
        kind = self.vars["type"].currentText()
        identifier = self.vars["id"].text().strip()
        try:
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]*", identifier):
                raise ValueError("Step ID must start with a letter and contain letters, digits, underscores or hyphens.")
            if any(s is not step and s["id"] == identifier for s in self.steps):
                raise ValueError("Another step already has that ID")
            updated = {"id": identifier, "type": kind}
            numeric = {"threshold": float, "retry_delay": float, "seconds": float, "retries": int,
                       **{key: int for key in ("x", "y", "x2", "y2", "duration", "offset_x", "offset_y")}}
            for name in FIELDS_BY_TYPE[kind]:
                widget = self.vars[name]
                if name == "click_match":
                    value = widget.isChecked()
                else:
                    raw = widget.text() if name == "text" else widget.text().strip()
                    if name == "region":
                        value = [int(part.strip()) for part in raw.split(",")] if raw else ""
                        if value and (len(value) != 4 or value[2] <= 0 or value[3] <= 0):
                            raise ValueError("Region must be x,y,width,height with positive dimensions")
                    elif name in numeric:
                        value = numeric[name](raw)
                        if not math.isfinite(value):
                            raise ValueError(f"{name} must be finite")
                    else:
                        value = raw
                updated[name] = value
            if kind == "find_image" and (not 0 < updated["threshold"] <= 1 or updated["retries"] < 1):
                raise ValueError("Similarity must be greater than 0 and at most 1; attempts must be at least 1")
            for name in ("seconds", "retry_delay", "duration"):
                if updated.get(name, 0) < 0:
                    raise ValueError(f"{name} cannot be negative")
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid step", str(exc))
            return False
        old_id = step["id"]
        step.clear()
        step.update(updated)
        if old_id != identifier:
            for other in self.steps:
                for branch in ("next", "on_true", "on_false"):
                    if other.get(branch) == old_id:
                        other[branch] = identifier
        self.form_dirty = False
        self.refresh_tree(identifier)
        return True

    def add_step(self):
        if self.form_dirty and not self.apply():
            return
        step = new_step(self.kind.currentText(), {s["id"] for s in self.steps})
        selected = self.get_selected()
        self.steps.insert(self.steps.index(selected) + 1 if selected else len(self.steps), step)
        self.refresh_tree(step["id"])

    def clone_step(self):
        if self.form_dirty and not self.apply():
            return
        selected = self.get_selected()
        if selected:
            step = copy.deepcopy(selected)
            step["id"] = new_step(step["type"], {s["id"] for s in self.steps})["id"]
            self.steps.insert(self.steps.index(selected) + 1, step)
            self.refresh_tree(step["id"])

    def delete_step(self):
        selected = self.get_selected()
        if not selected:
            return
        if any(s is not selected and selected["id"] in [s.get(k) for k in ("next", "on_true", "on_false")] for s in self.steps):
            QMessageBox.warning(self, "Referenced step", "Remove incoming branch links before deleting this step")
            return
        self.form_dirty = False
        self.steps.remove(selected)
        self.refresh_tree(self.steps[0]["id"] if self.steps else None)

    def move(self, direction):
        if self.form_dirty and not self.apply():
            return
        selected = self.get_selected()
        if selected:
            index = self.steps.index(selected)
            other = index + direction
            if 0 <= other < len(self.steps):
                self.steps[index], self.steps[other] = self.steps[other], self.steps[index]
                self.refresh_tree(selected["id"])

    def show_ids(self):
        QMessageBox.information(self, "Step IDs", "\n".join(s["id"] for s in self.steps))

    def relative_image(self, path):
        return Path(os.path.relpath(path, self.workflow_path.parent)).as_posix() if path.anchor == self.workflow_path.anchor else str(path)

    def browse_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Template image", str(self.workflow_path.parent), "Images (*.png *.jpg *.jpeg *.bmp)")
        if path:
            self.vars["image"].setText(self.relative_image(Path(path)))

    def write_workflow(self, target):
        snapshot = copy.deepcopy(self.steps)
        if target.parent != self.workflow_path.parent:
            for step in snapshot:
                image = step.get("image")
                if image and not Path(image).is_absolute():
                    step["image"] = str((self.workflow_path.parent / image).resolve())
        try:
            validate(snapshot)
            target.parent.mkdir(parents=True, exist_ok=True)
            save_workflow(target, snapshot)
        except (OSError, AutomationError) as exc:
            QMessageBox.warning(self, "Save failed", str(exc))
            return False
        self.steps = snapshot
        self.workflow_path = target
        self.on_select()
        self.logging(f"Saved: {target}")
        return True

    def save(self):
        if self.running or self.busy or (self.get_selected() and not self.apply()):
            return
        self.write_workflow(self.workflow_path)

    def save_as(self):
        if self.running or self.busy or (self.get_selected() and not self.apply()):
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save workflow", str(self.workflow_path), "Workflow JSON (*.json)")
        if path:
            self.write_workflow(Path(path))

    def open_file(self):
        if self.running or self.busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open workflow", str(self.workflow_path.parent), "Workflow JSON (*.json)")
        if not path:
            return
        try:
            steps = load_workflow(Path(path))
        except (OSError, ValueError, AutomationError, KeyError, TypeError, AttributeError) as exc:
            QMessageBox.warning(self, "Open failed", str(exc))
            return
        self.form_dirty = False
        self.steps, self.workflow_path = steps, Path(path)
        self.refresh_tree(steps[0]["id"])

    def run(self):
        if self.running or self.busy or self.closing:
            return
        if self.get_selected() and not self.apply():
            return
        try:
            validate(self.steps)
            adb = self.current_adb()
        except AutomationError as exc:
            QMessageBox.warning(self, "Cannot run", str(exc))
            return
        self.stop_event.clear()
        self.running = True
        self.set_controls()
        snapshot, folder = copy.deepcopy(self.steps), self.workflow_path.parent
        def worker():
            status = "Completed"
            try:
                run_workflow(snapshot, adb, folder, self.stop_event, self.logging,
                             lambda step_id: self.messages.put(("step", step_id)))
            except InterruptedError:
                status = "Stopped by user"
            except Exception as exc:
                status = "Failed — see execution log"
                self.logging(f"ERROR: {exc}")
            finally:
                self.messages.put(("done", status))
        self.launch(worker)

    def stop(self):
        if self.running:
            self.stop_event.set()
            self.run_state.setText("Stopping…")
            self.statusBar().showMessage("Stopping after the current ADB operation…")

    def closeEvent(self, event):
        self.stop_event.set()
        if self.hotkey_listener:
            self.hotkey_listener.stop()
            self.hotkey_listener = None
        if any(thread.is_alive() for thread in self.workers):
            self.closing = True
            self.set_controls()
            self.statusBar().showMessage("Stopping device operations before closing…")
            event.ignore()
            return
        self.timer.stop()
        self.platform_style.close()
        event.accept()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-test", action="store_true", help="Initialize Qt without device access and exit")
    args = parser.parse_args()
    app = QApplication(sys.argv)
    app.setApplicationName("AndroidVisualAutoclicker")
    app.setOrganizationName("AndroidVisualAutoclicker")
    window = Editor(startup=not args.smoke_test)
    if args.smoke_test:
        window.ensurePolished()
        if window.grab().isNull():
            return 1
        QTimer.singleShot(50, app.quit)
    else:
        window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
