"""Build on each target OS: python build_app.py (after installing build requirements)."""
import os
import sys
from pathlib import Path

import PyInstaller.__main__


def main():
    root = Path(__file__).resolve().parent
    if sys.platform == "win32":
        # Unrelated tools on PATH can supply incompatible DLLs (notably ICU).
        # Keep dependency discovery limited to Python, Qt's own paths, and Windows.
        windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        os.environ["PATH"] = os.pathsep.join(map(str, [Path(sys.executable).parent,
                                                     windows / "System32", windows]))
    backend = {"win32": "win32", "darwin": "darwin"}.get(sys.platform, "xorg")
    PyInstaller.__main__.run([
        "--noconfirm", "--clean", "--onedir", "--windowed", "--noupx",
        "--name", "AndroidVisualAutoclicker",
        "--distpath", str(root / "dist"), "--workpath", str(root / "build"),
        "--specpath", str(root / "build"),
        "--hidden-import", f"pynput.keyboard._{backend}",
        "--hidden-import", f"pynput.mouse._{backend}",
        "--exclude-module", "tkinter", "--exclude-module", "PyQt5",
        "--exclude-module", "PyQt6", "--exclude-module", "PySide2",
        str(root / "app.py"),
    ])


if __name__ == "__main__":
    main()
