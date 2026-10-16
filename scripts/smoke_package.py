"""Verify the frozen application can initialize Qt without a connected device."""
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "dist"
name = "AndroidVisualAutoclicker"
if sys.platform == "darwin":
    executable = root / f"{name}.app" / "Contents" / "MacOS" / name
else:
    executable = root / name / (f"{name}.exe" if sys.platform == "win32" else name)
subprocess.run([str(executable), "--smoke-test"], check=True, timeout=30,
               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
print("Packaged Qt application initialized and exited successfully.")
