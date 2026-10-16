"""ADB-powered image-driven workflow runner. No UI dependencies."""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np
from PIL import Image


class AutomationError(RuntimeError):
    pass


class ADB:
    def __init__(self, serial: str = "", executable: str = "adb", timeout: int = 12):
        self.serial = serial
        folder = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
        portable_adb = folder / "platform-tools" / "adb.exe"
        self.executable = str(portable_adb) if executable == "adb" and portable_adb.is_file() else executable
        self.timeout = timeout

    def command(self, *parts: str, binary: bool = False):
        args = [self.executable]
        if self.serial:
            args += ["-s", self.serial]
        args += [str(p) for p in parts]
        try:
            out = subprocess.run(args, capture_output=True, timeout=self.timeout, check=False,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError as exc:
            raise AutomationError("ADB not found. Put the Android platform-tools folder beside the app, or add adb to PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise AutomationError("ADB command timed out") from exc
        if out.returncode:
            raise AutomationError((out.stderr or out.stdout).decode("utf-8", "replace").strip() or "ADB command failed")
        return out.stdout if binary else out.stdout.decode("utf-8", "replace")

    def devices(self):
        output = self.command("devices")
        return [line.split()[0] for line in output.splitlines()[1:] if len(line.split()) >= 2 and line.split()[1] == "device"]

    def screenshot(self) -> Image.Image:
        data = self.command("exec-out", "screencap", "-p", binary=True)
        try:
            return Image.open(io.BytesIO(data)).convert("RGB")
        except Exception as exc:
            raise AutomationError("Could not decode device screenshot") from exc

    def tap(self, x: int, y: int):
        self.command("shell", "input", "tap", str(x), str(y))

    def swipe(self, x: int, y: int, x2: int, y2: int, duration: int):
        self.command("shell", "input", "swipe", str(x), str(y), str(x2), str(y2), str(duration))

    def key(self, code: str):
        if not re.fullmatch(r"[A-Za-z0-9_]+", code):
            raise AutomationError("Key code may only contain letters, digits and underscores")
        self.command("shell", "input", "keyevent", code)

    def text(self, value: str):
        # Android's input text command supports limited Unicode / special characters.
        if not all(32 <= ord(ch) <= 126 for ch in value):
            raise AutomationError("ADB text input supports basic ASCII only")
        escaped = value.replace(" ", "%s")
        self.command("shell", "input", "text", escaped)


def match_image(screen: Image.Image, template: Image.Image, threshold: float = 0.88, region=None):
    """Return (score, absolute center x, absolute center y), or (score, None, None)."""
    full = np.array(screen.convert("RGB"))
    xoffset = yoffset = 0
    if region:
        x, y, w, h = [int(v) for v in region]
        if w <= 0 or h <= 0:
            raise AutomationError("Search region width and height must be positive")
        xoffset, yoffset = max(x, 0), max(y, 0)
        full = full[yoffset:max(yoffset, y + h), xoffset:max(xoffset, x + w)]
    small = np.array(template.convert("RGB"))
    if not full.size or small.shape[0] > full.shape[0] or small.shape[1] > full.shape[1]:
        return 0.0, None, None
    img = cv2.cvtColor(full, cv2.COLOR_RGB2GRAY)
    tpl = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    if float(tpl.std()) < 1.0:
        # Constant templates are unsuitable for normalized correlation.
        raise AutomationError("Template has too little visual detail; capture a larger area")
    scores = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
    _, highest, _, position = cv2.minMaxLoc(scores)
    if highest < threshold:
        return float(highest), None, None
    return float(highest), xoffset + position[0] + tpl.shape[1] // 2, yoffset + position[1] + tpl.shape[0] // 2


def validate(steps: list[dict]):
    if not steps:
        raise AutomationError("Workflow is empty")
    ids = [str(s.get("id", "")).strip() for s in steps]
    if any(not value for value in ids) or len(set(ids)) != len(ids):
        raise AutomationError("Every step must have a unique, non-empty ID")
    for step in steps:
        kind = step.get("type")
        if kind not in {"find_image", "tap", "wait", "swipe", "key", "text", "goto", "stop"}:
            raise AutomationError(f"Unknown action: {kind}")
        for branch in ("next", "on_true", "on_false"):
            target = step.get(branch, "")
            if target and target not in ids:
                raise AutomationError(f"Step {step['id']} links to missing step {target}")
        if kind == "find_image" and not step.get("image"):
            raise AutomationError(f"Step {step['id']} needs a template image")


def run_workflow(steps: list[dict], adb: ADB, folder: Path, stop_event: threading.Event,
                 log: Callable[[str], None], step_changed: Callable[[str], None] = lambda _id: None,
                 max_executions: int = 10000):
    validate(steps)
    by_id = {step["id"]: step for step in steps}
    order = {step["id"]: steps[i + 1]["id"] if i + 1 < len(steps) else "" for i, step in enumerate(steps)}
    current = steps[0]["id"]
    executions = 0

    def pause(seconds: float):
        if stop_event.wait(max(0.0, seconds)):
            raise InterruptedError("Stopped by user")

    while current and not stop_event.is_set():
        executions += 1
        if executions > max_executions:
            raise AutomationError("Maximum step executions reached (possible endless loop)")
        step = by_id[current]
        kind = step["type"]
        step_changed(current)
        log(f"[{current}] {kind}")
        following = step.get("next") or order[current]
        if kind == "stop":
            break
        elif kind == "goto":
            following = step.get("next", "")
        elif kind == "wait":
            pause(float(step.get("seconds", 1)))
        elif kind == "tap":
            adb.tap(int(step.get("x", 0)), int(step.get("y", 0)))
        elif kind == "swipe":
            adb.swipe(int(step.get("x", 0)), int(step.get("y", 0)), int(step.get("x2", 0)), int(step.get("y2", 0)), int(step.get("duration", 400)))
        elif kind == "key":
            adb.key(str(step.get("key", "KEYCODE_BACK")))
        elif kind == "text":
            adb.text(str(step.get("text", "")))
        elif kind == "find_image":
            path = Path(step["image"])
            if not path.is_absolute():
                path = folder / path
            if not path.is_file():
                raise AutomationError(f"Template not found: {path}")
            template = Image.open(path).convert("RGB")
            threshold = float(step.get("threshold", 0.88))
            retries = max(1, int(step.get("retries", 5)))
            delay = max(0.0, float(step.get("retry_delay", 0.8)))
            region = step.get("region") or None
            found = False
            for attempt in range(1, retries + 1):
                if stop_event.is_set():
                    raise InterruptedError("Stopped by user")
                score, center_x, center_y = match_image(adb.screenshot(), template, threshold, region)
                log(f"  attempt {attempt}/{retries}, similarity {score:.3f}")
                if center_x is not None:
                    found = True
                    if step.get("click_match", True):
                        adb.tap(center_x + int(step.get("offset_x", 0)), center_y + int(step.get("offset_y", 0)))
                        log(f"  tapped at ({center_x}, {center_y}) + offset")
                    break
                if attempt < retries:
                    pause(delay)
            log("  FOUND" if found else "  NOT FOUND")
            following = (step.get("on_true") if found else step.get("on_false")) or following
        current = following
    if stop_event.is_set():
        raise InterruptedError("Stopped by user")
    log("Workflow completed")


def load_workflow(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    steps = data["steps"] if isinstance(data, dict) else data
    validate(steps)
    return steps


def save_workflow(path: Path, steps: list[dict]):
    validate(steps)
    path.write_text(json.dumps({"version": 1, "steps": steps}, indent=2), encoding="utf-8")
