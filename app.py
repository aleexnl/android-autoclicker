"""Desktop workflow designer for Android visual automation."""
from __future__ import annotations

import copy
import json
import os
import queue
import re
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from pynput import keyboard
import sv_ttk
import darkdetect

from PIL import Image, ImageTk

from engine import ADB, AutomationError, load_workflow, run_workflow, save_workflow

ROOT = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):
    ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AndroidVisualAutoclicker"
TEMPLATES = ROOT / "templates"
TEMPLATES.mkdir(parents=True, exist_ok=True)
SETTINGS_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".config")) / "AndroidVisualAutoclicker" / "settings.json"
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

FIELDS = [
    ("id", "Step ID"), ("type", "Action"), ("image", "Image file"),
    ("threshold", "Similarity (0..1)"), ("retries", "Attempts"),
    ("retry_delay", "Retry delay (s)"), ("click_match", "Tap matched image"),
    ("offset_x", "Tap offset X"), ("offset_y", "Tap offset Y"),
    ("region", "Region x,y,w,h"),
    ("on_true", "If found: step ID"), ("on_false", "If missing: step ID"),
    ("next", "Next step ID"), ("seconds", "Wait seconds"),
    ("x", "X"), ("y", "Y"), ("x2", "End X"), ("y2", "End Y"),
    ("duration", "Swipe milliseconds"), ("key", "Android key code"),
    ("text", "Input text"),
]
FIELDS_BY_TYPE = {
    "find_image": ["image", "threshold", "retries", "retry_delay", "click_match", "offset_x", "offset_y", "region", "on_true", "on_false", "next"],
    "tap": ["x", "y", "next"], "wait": ["seconds", "next"],
    "swipe": ["x", "y", "x2", "y2", "duration", "next"],
    "key": ["key", "next"], "text": ["text", "next"],
    "goto": ["next"], "stop": [],
}
DEFAULTS = {
    "find_image": {"image": "", "threshold": 0.88, "retries": 5, "retry_delay": 0.8, "click_match": True, "offset_x": 0, "offset_y": 0, "region": "", "on_true": "", "on_false": "", "next": ""},
    "tap": {"x": 200, "y": 200, "next": ""},
    "wait": {"seconds": 1.0, "next": ""},
    "swipe": {"x": 250, "y": 900, "x2": 250, "y2": 300, "duration": 450, "next": ""},
    "key": {"key": "KEYCODE_BACK", "next": ""},
    "text": {"text": "hello", "next": ""},
    "goto": {"next": ""}, "stop": {},
}


def new_step(kind, taken):
    i = 1
    while f"step_{i}" in taken:
        i += 1
    return {"id": f"step_{i}", "type": kind, **copy.deepcopy(DEFAULTS[kind])}


class CropDialog(tk.Toplevel):
    def __init__(self, parent, image: Image.Image, save_to: Path, callback):
        super().__init__(parent)
        self.title("Drag to select template region")
        self.image = image
        self.save_to = save_to
        self.callback = callback
        self.start = None
        self.rect = None
        self.scale = min(1.0, 900 / image.width, 650 / image.height)
        resized = image.resize((round(image.width * self.scale), round(image.height * self.scale)))
        self.photo = ImageTk.PhotoImage(resized)
        self.canvas = tk.Canvas(self, width=resized.width, height=resized.height, cursor="crosshair", highlightthickness=0)
        self.canvas.pack(padx=10, pady=10)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.canvas.bind("<ButtonPress-1>", self.down)
        self.canvas.bind("<B1-Motion>", self.move)
        self.canvas.bind("<ButtonRelease-1>", self.up)
        ttk.Label(self, text="Drag a rectangle around the feature to recognize; avoid large uniform areas.").pack(pady=(0, 10))
        self.transient(parent)
        self.grab_set()

    def down(self, event):
        self.start = (event.x, event.y)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#00e5ff", width=2)

    def move(self, event):
        if self.start:
            self.canvas.coords(self.rect, *self.start, event.x, event.y)

    def up(self, event):
        if not self.start:
            return
        x1, x2 = sorted((self.start[0], event.x))
        y1, y2 = sorted((self.start[1], event.y))
        self.start = None
        if x2 - x1 < 12 or y2 - y1 < 12:
            messagebox.showwarning("Selection too small", "Select an area at least 12 pixels wide and tall.", parent=self)
            return
        box = tuple(int(value / self.scale) for value in (x1, y1, x2, y2))
        crop = self.image.crop(box)
        self.save_to.parent.mkdir(parents=True, exist_ok=True)
        crop.save(self.save_to)
        self.callback(self.save_to)
        self.destroy()


class Editor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.appearance = tk.StringVar(value=load_appearance())
        self.system_theme = "light"
        self.active_theme = None
        self.theme_stop = threading.Event()
        self.apply_appearance()
        self.title("Android Visual Autoclicker — ADB Workflow Studio")
        self.geometry("1280x860")
        self.minsize(1100, 720)
        self.workflow_path = ROOT / "example_workflow.json"
        self.steps = [new_step("find_image", set()), new_step("wait", {"step_1"}), new_step("stop", {"step_1", "step_2"})]
        self.current = None
        self.running = False
        self.stop_event = threading.Event()
        self.messages = queue.Queue()
        self.adb = ADB()
        self.vars = {}
        self.widgets = {}
        self._build()
        self.apply_appearance()
        threading.Thread(target=self.watch_system_theme, daemon=True).start()
        self.refresh_tree()
        self.tree.selection_set(self.steps[0]["id"])
        self.on_select()
        self.after(100, self.process_messages)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind_all("<F8>", lambda _e: self.stop())
        self.bind_all("<Escape>", lambda _e: self.stop() if self.running else None)
        self.hotkey_listener = keyboard.Listener(on_press=self.global_key)
        self.hotkey_listener.daemon = True
        self.hotkey_listener.start()

    def watch_system_theme(self):
        # OS detection stays off the Tk thread; all widget changes use the queue.
        while not self.theme_stop.is_set():
            try:
                theme = "dark" if darkdetect.isDark() else "light"
            except Exception:
                theme = "light"
            self.messages.put(("system_theme", theme))
            self.theme_stop.wait(2)

    def change_appearance(self, _event=None):
        self.apply_appearance()
        try:
            save_appearance(self.appearance.get())
        except OSError as exc:
            messagebox.showwarning("Appearance not saved", f"The theme was applied, but could not be saved for next time.\n{exc}", parent=self)

    def apply_appearance(self):
        preference = self.appearance.get()
        theme = self.system_theme if preference == "System" else preference.lower()
        if theme != self.active_theme:
            sv_ttk.set_theme(theme)
            self.active_theme = theme
        dark = theme == "dark"
        style = ttk.Style(self)
        style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Section.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Muted.TLabel", foreground="#bdbdbd" if dark else "#616161")
        style.configure("Treeview", rowheight=34)
        style.configure("Compact.TButton", padding=(8, 5), width=3)
        if hasattr(self, "form_canvas"):
            self.form_canvas.configure(bg=self.cget("background"))
        if hasattr(self, "log_view"):
            foreground = "#eeeeee" if dark else "#323130"
            self.log_view.configure(bg="#202020" if dark else "#ffffff", fg=foreground,
                                    insertbackground=foreground,
                                    selectbackground="#315d80" if dark else "#cce4f7",
                                    selectforeground="#ffffff" if dark else "#1a1a1a")

    def _build(self):
        main = ttk.Frame(self, padding=20)
        main.pack(fill="both", expand=True)
        header = ttk.Frame(main)
        header.pack(fill="x", pady=(0, 16))
        ttk.Label(header, text="Workflow Studio", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="Appearance:").pack(side="left", padx=(24, 6))
        appearance_combo = ttk.Combobox(header, textvariable=self.appearance, values=APPEARANCES, state="readonly", width=9)
        appearance_combo.pack(side="left")
        appearance_combo.bind("<<ComboboxSelected>>", self.change_appearance)
        ttk.Button(header, text="Save as", command=self.save_as).pack(side="right")
        ttk.Button(header, text="Save", command=self.save).pack(side="right", padx=6)
        ttk.Button(header, text="Open", command=self.open_file).pack(side="right")
        toolbar = ttk.Frame(main)
        toolbar.pack(fill="x", pady=(0, 16))
        ttk.Label(toolbar, text="Device:").pack(side="left")
        self.device = tk.StringVar()
        self.device_combo = ttk.Combobox(toolbar, textvariable=self.device, width=23)
        self.device_combo.pack(side="left", padx=5)
        ttk.Button(toolbar, text="Refresh", command=self.refresh_devices).pack(side="left")
        ttk.Button(toolbar, text="Connect MuMu", command=self.connect_mumu).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Capture template", command=self.capture).pack(side="left", padx=8)
        self.start_btn = ttk.Button(toolbar, text="▶ Run (F6)", style="Accent.TButton", command=self.run)
        self.start_btn.pack(side="right", padx=4)
        self.stop_btn = ttk.Button(toolbar, text="■ STOP (F8)", command=self.stop)
        self.stop_btn.pack(side="right", padx=4)
        self.bind_all("<F6>", lambda _e: self.run())

        split = ttk.Panedwindow(main, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split, padding=(0, 0, 12, 0))
        right = ttk.Frame(split, padding=(12, 0, 0, 0))
        split.add(left, weight=5)
        split.add(right, weight=4)
        ttk.Label(left, text="Workflow steps", style="Section.TLabel").pack(anchor="w", pady=(0, 12))
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=("action", "flow"), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="ID")
        self.tree.heading("action", text="Action")
        self.tree.heading("flow", text="Transitions")
        self.tree.column("#0", width=125, stretch=False)
        self.tree.column("action", width=112, stretch=False)
        self.tree.column("flow", width=220)
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.on_select())
        row = ttk.Frame(left)
        row.pack(fill="x", pady=7)
        self.kind = tk.StringVar(value="find_image")
        ttk.Combobox(row, textvariable=self.kind, values=list(DEFAULTS), state="readonly", width=15).pack(side="left")
        for label, command in [("+ Add", self.add_step), ("Clone", self.clone_step), ("Delete", self.delete_step), ("↑", lambda: self.move(-1)), ("↓", lambda: self.move(1))]:
            ttk.Button(row, text=label, command=command, style="Compact.TButton" if label in {"↑", "↓"} else "TButton", width=3 if label in {"↑", "↓"} else 6).pack(side="left", padx=2)
        ttk.Label(left, text="Use ↑ ↓ to reorder. Branches refer to step IDs.\nBlank next continues to the following row.\nThe highlighted row shows the executing step.", style="Muted.TLabel", justify="left").pack(anchor="w", pady=(4, 0))

        ttk.Label(right, text="Step configuration", style="Section.TLabel").pack(anchor="w", pady=(0, 12))
        canvas = tk.Canvas(right, highlightthickness=0, bg=self.cget("background"))
        self.form_canvas = canvas
        scrollbar = ttk.Scrollbar(right, orient="vertical", command=canvas.yview)
        self.form = ttk.Frame(canvas)
        self.form.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.form, anchor="nw", tags="content")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure("content", width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        # Scope wheel scrolling to this editor, preserving dropdown/list scrolling.
        def scroll_form(event):
            if event.widget.winfo_class() != "TCombobox" and str(event.widget).startswith(str(right)):
                canvas.yview_scroll(-int(event.delta / 120), "units")
        self.bind("<MouseWheel>", scroll_form, add="+")
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for n, (name, label) in enumerate(FIELDS):
            wrap = ttk.Frame(self.form)
            self.widgets[name] = wrap
            ttk.Label(wrap, text=label, width=21).pack(side="left", padx=(5, 4))
            variable = tk.StringVar()
            self.vars[name] = variable
            if name == "type":
                widget = ttk.Combobox(wrap, textvariable=variable, values=list(DEFAULTS), state="readonly")
                widget.bind("<<ComboboxSelected>>", lambda _e: self.change_type())
            elif name == "click_match":
                widget = ttk.Combobox(wrap, textvariable=variable, values=["True", "False"], state="readonly")
            else:
                widget = ttk.Entry(wrap, textvariable=variable)
            widget.pack(side="left", fill="x", expand=True, padx=(0, 5))
            if name == "image":
                ttk.Button(wrap, text="Browse", command=self.browse_image).pack(side="right")
            if name == "region":
                ttk.Button(wrap, text="Clear", command=lambda: self.vars["region"].set("")).pack(side="right")
            if name in {"on_true", "on_false", "next"}:
                ttk.Button(wrap, text="IDs", command=self.show_ids).pack(side="right")
        ttk.Button(self.form, text="Apply changes to selected step", style="Accent.TButton", command=self.apply).pack(fill="x", padx=8, pady=12)

        bottom = ttk.LabelFrame(main, text="Execution log", padding=10)
        bottom.pack(fill="x", pady=(16, 0))
        self.log_view = tk.Text(bottom, height=6, bg="#ffffff", fg="#323130", insertbackground="#323130", selectbackground="#cce4f7", selectforeground="#1a1a1a", relief="flat", borderwidth=0, highlightthickness=0, padx=10, pady=8, font=("Consolas", 10), state="disabled")
        log_scroll = ttk.Scrollbar(bottom, orient="vertical", command=self.log_view.yview)
        self.log_view.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_view.pack(side="left", fill="both", expand=True)
        self.status = tk.StringVar(value="Idle — connect Android device or start an emulator, then press Refresh.")
        ttk.Label(main, textvariable=self.status, style="Muted.TLabel").pack(anchor="w", pady=(10, 0))
        self.refresh_devices()

    def logging(self, msg):
        self.messages.put(("log", str(msg)))

    def process_messages(self):
        try:
            while True:
                event, value = self.messages.get_nowait()
                if event == "log":
                    self.log_view.configure(state="normal")
                    self.log_view.insert("end", value + "\n")
                    self.log_view.see("end")
                    self.log_view.configure(state="disabled")
                elif event == "step":
                    if self.tree.exists(value):
                        self.tree.selection_set(value)
                        self.tree.see(value)
                    self.status.set(f"Running step {value} — F8 to stop")
                elif event == "done":
                    self.running = False
                    self.start_btn.configure(state="normal")
                    self.status.set(value)
                elif event == "system_theme":
                    self.system_theme = value
                    if self.appearance.get() == "System" and self.active_theme != value:
                        self.apply_appearance()
        except queue.Empty:
            pass
        self.after(100, self.process_messages)

    def global_key(self, key):
        if key == keyboard.Key.f8:
            # Listener runs off the Tk main thread: only set the thread-safe event.
            self.stop_event.set()
            if self.running:
                self.messages.put(("log", "Global F8 emergency stop requested"))

    def connect_mumu(self):
        port = simpledialog.askstring("Connect MuMu", "ADB port shown in MuMu Issues Diagnostic / multi-instance manager:", initialvalue="16384", parent=self)
        if port is None:
            return
        port = port.strip()
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            messagebox.showerror("Invalid port", "Enter a valid numeric port (1-65535)")
            return
        serial = f"127.0.0.1:{port}"
        try:
            result = ADB().command("connect", serial)
            self.logging(result.strip())
            self.refresh_devices()
            if serial in self.device_combo.cget("values"):
                self.device.set(serial)
            else:
                messagebox.showwarning("Connection not ready", f"{serial} did not appear as a connected device. Check the ADB port in MuMu and try again.")
        except AutomationError as exc:
            messagebox.showerror("ADB failed", str(exc))

    def current_adb(self):
        return ADB(serial=self.device.get().strip())

    def refresh_devices(self):
        try:
            found = ADB().devices()
            self.device_combo.configure(values=found)
            if found and self.device.get() not in found:
                self.device.set(found[0])
            self.logging("Devices: " + (", ".join(found) or "none connected"))
        except AutomationError as exc:
            self.logging(str(exc))

    def refresh_tree(self, select=None):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for step in self.steps:
            transitions = []
            for field, tag in [("on_true", "✓"), ("on_false", "✗"), ("next", "→")]:
                if step.get(field):
                    transitions.append(f"{tag} {step[field]}")
            self.tree.insert("", "end", iid=step["id"], text=step["id"], values=(step["type"], "  ".join(transitions) or "↓ next row"))
        if select and self.tree.exists(select):
            self.tree.selection_set(select)

    def get_selected(self):
        selected = self.tree.selection()
        return next((s for s in self.steps if selected and s["id"] == selected[0]), None)

    def on_select(self):
        step = self.get_selected()
        self.current = step["id"] if step else None
        if not step:
            return
        for name in self.vars:
            self.vars[name].set(str(step.get(name, "")))
        self.show_fields(step["type"])

    def show_fields(self, kind):
        for widget in self.widgets.values():
            widget.pack_forget()
        for name in ["id", "type", *FIELDS_BY_TYPE.get(kind, [])]:
            self.widgets[name].pack(fill="x", pady=4, padx=3)

    def change_type(self):
        self.show_fields(self.vars["type"].get())

    def apply(self):
        step = self.get_selected()
        if not step:
            return False
        kind = self.vars["type"].get()
        identifier = self.vars["id"].get().strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]*", identifier):
            messagebox.showerror("Invalid ID", "Use letters, digits, underscores or hyphens, starting with a letter.")
            return False
        if identifier != step["id"] and any(s["id"] == identifier for s in self.steps):
            messagebox.showerror("Duplicate ID", "Another step already has that ID")
            return False
        new = {"id": identifier, "type": kind, **copy.deepcopy(DEFAULTS[kind])}
        numeric = {"threshold": float, "retry_delay": float, "seconds": float, "retries": int, "x": int, "y": int, "x2": int, "y2": int, "duration": int, "offset_x": int, "offset_y": int}
        try:
            for name in FIELDS_BY_TYPE[kind]:
                raw = self.vars[name].get().strip()
                if name == "click_match":
                    new[name] = raw == "True"
                elif name == "region":
                    new[name] = [int(s.strip()) for s in raw.split(",")] if raw else ""
                    if new[name] and len(new[name]) != 4:
                        raise ValueError("Region must have four numbers")
                elif name in numeric:
                    new[name] = numeric[name](raw)
                else:
                    new[name] = raw
            if kind == "find_image" and not (0 < new["threshold"] <= 1):
                raise ValueError("Similarity must be between 0 and 1")
            if kind == "find_image" and new["retries"] < 1:
                raise ValueError("Attempts must be at least 1")
        except ValueError as exc:
            messagebox.showerror("Invalid value", str(exc))
            return False
        old_id = step["id"]
        step.clear()
        step.update(new)
        if old_id != identifier:
            for s in self.steps:
                for key in ("next", "on_true", "on_false"):
                    if s.get(key) == old_id:
                        s[key] = identifier
        self.refresh_tree(identifier)
        self.logging(f"Updated {identifier}")
        return True

    def add_step(self):
        step = new_step(self.kind.get(), {s["id"] for s in self.steps})
        selected = self.get_selected()
        index = self.steps.index(selected) + 1 if selected else len(self.steps)
        self.steps.insert(index, step)
        self.refresh_tree(step["id"])
        self.on_select()

    def clone_step(self):
        selected = self.get_selected()
        if not selected:
            return
        step = copy.deepcopy(selected)
        step["id"] = new_step(step["type"], {s["id"] for s in self.steps})["id"]
        self.steps.insert(self.steps.index(selected) + 1, step)
        self.refresh_tree(step["id"])
        self.on_select()

    def delete_step(self):
        selected = self.get_selected()
        if not selected:
            return
        if any(s is not selected and selected["id"] in [s.get("next"), s.get("on_true"), s.get("on_false")] for s in self.steps):
            messagebox.showwarning("Referenced step", "Remove incoming branch links before deleting this step")
            return
        self.steps.remove(selected)
        self.refresh_tree(self.steps[0]["id"] if self.steps else None)
        self.on_select()

    def move(self, direction):
        selected = self.get_selected()
        if not selected:
            return
        index = self.steps.index(selected)
        other = index + direction
        if 0 <= other < len(self.steps):
            self.steps[index], self.steps[other] = self.steps[other], self.steps[index]
            self.refresh_tree(selected["id"])

    def show_ids(self):
        messagebox.showinfo("Available step IDs", "\n".join(s["id"] for s in self.steps))

    def browse_image(self):
        path = filedialog.askopenfilename(filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp")])
        if path:
            self.vars["image"].set(self.relative_image(Path(path)))

    def relative_image(self, path):
        try:
            return str(path.relative_to(self.workflow_path.parent))
        except ValueError:
            return str(path)

    def capture(self):
        try:
            self.status.set("Capturing device screen...")
            self.update_idletasks()
            image = self.current_adb().screenshot()
        except AutomationError as exc:
            messagebox.showerror("Screenshot failed", str(exc))
            return
        target = filedialog.asksaveasfilename(initialdir=TEMPLATES, initialfile="target.png", defaultextension=".png", filetypes=[("PNG", "*.png")])
        if target:
            CropDialog(self, image, Path(target), lambda path: self.vars["image"].set(self.relative_image(path)))
        self.status.set("Ready")

    def save(self):
        if self.get_selected() and not self.apply():
            return
        try:
            save_workflow(self.workflow_path, self.steps)
            self.logging(f"Saved: {self.workflow_path}")
        except (AutomationError, OSError) as exc:
            messagebox.showerror("Save error", str(exc))

    def save_as(self):
        path = filedialog.asksaveasfilename(initialdir=self.workflow_path.parent, defaultextension=".json", filetypes=[("Workflow JSON", "*.json")])
        if path:
            # Keep existing templates pointing to the same file if the folder changes.
            old_folder = self.workflow_path.parent
            for step in self.steps:
                image = step.get("image")
                if image and not Path(image).is_absolute():
                    step["image"] = str((old_folder / image).resolve())
            self.workflow_path = Path(path)
            self.save()

    def open_file(self):
        path = filedialog.askopenfilename(filetypes=[("Workflow JSON", "*.json")])
        if not path:
            return
        try:
            steps = load_workflow(Path(path))
        except (AutomationError, OSError, ValueError, KeyError) as exc:
            messagebox.showerror("Open error", str(exc))
            return
        self.workflow_path = Path(path)
        self.steps = steps
        self.refresh_tree(steps[0]["id"])
        self.on_select()
        self.logging(f"Loaded: {path}")

    def run(self):
        if self.running:
            return
        if self.get_selected() and not self.apply():
            return
        if not self.device.get().strip():
            messagebox.showwarning("No device", "Choose a connected ADB device first")
            return
        try:
            from engine import validate
            validate(self.steps)
        except AutomationError as exc:
            messagebox.showerror("Workflow error", str(exc))
            return
        self.stop_event.clear()
        self.running = True
        self.start_btn.configure(state="disabled")
        snapshot = copy.deepcopy(self.steps)
        adb = self.current_adb()
        folder = self.workflow_path.parent
        self.logging("========== Starting workflow ==========")
        def worker():
            status = "Completed"
            try:
                run_workflow(snapshot, adb, folder, self.stop_event, self.logging,
                             lambda step_id: self.messages.put(("step", step_id)))
            except InterruptedError:
                status = "Stopped by user"
                self.logging(status)
            except Exception as exc:
                status = "Failed — see execution log"
                self.logging(f"ERROR: {exc}")
            finally:
                self.messages.put(("done", status))
        threading.Thread(target=worker, daemon=True).start()

    def stop(self):
        if self.running:
            self.stop_event.set()
            self.logging("Emergency stop requested. Current ADB call may take up to its timeout to return.")

    def close(self):
        self.theme_stop.set()
        self.stop_event.set()
        self.hotkey_listener.stop()
        self.destroy()


if __name__ == "__main__":
    Editor().mainloop()
