# Android Visual Autoclicker

A desktop Python/Tkinter workflow editor for controlling **your Android phone or emulator via ADB**. It supports OpenCV screenshot matching, conditional branching, retry limits, clicks, swipes, key presses, text input, loops, JSON saving and an emergency stop.

The editor uses the [Sun Valley ttk theme](https://github.com/rdbende/Sun-Valley-ttk-theme) for Windows 11-style controls, with separate file and device toolbars, scrollable workflow/configuration panels, and a light execution log. The theme is included in the Windows executable; source users should reinstall `requirements.txt` when updating.

## Run the Windows executable

Double-click `AndroidVisualAutoclicker.exe`. Users do not need Python, pip, or a terminal.

Download and extract [Android SDK Platform Tools for Windows](https://developer.android.com/tools/releases/platform-tools), then put the entire `platform-tools` folder beside the executable (including its DLLs). An existing `adb` on PATH also works.

```text
AndroidVisualAutoclicker.exe
platform-tools/
    adb.exe
    AdbWinApi.dll
    AdbWinUsbApi.dll
    ...
```

Connect a device with USB debugging enabled or start your emulator, then click **Refresh** or **Connect MuMu**. Authorize debugging on the phone when prompted.

The executable stores its default workflow and captured templates in `%LOCALAPPDATA%\AndroidVisualAutoclicker`. **Open** and **Save as** can use other folders. Keep workflow JSON files together with their referenced template images when sharing them.

## Build the Windows executable (maintainers)

On Windows with Python 3.10+ installed, run from the project folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_windows.ps1
```

The script installs dependencies in `.venv-build` and uses PyInstaller to produce `dist\AndroidVisualAutoclicker.exe` with Python, Tkinter and the required libraries included. Share that executable with users. Android Platform Tools are installed separately as described above. The first launch may take a few seconds while the executable extracts its libraries. Build on the Windows architecture you intend to support.

## Requirements (running from source)

- Windows 10/11 (also works on desktop Linux/macOS with Tkinter installed)
- Python 3.10+
- [Android SDK Platform Tools](https://developer.android.com/tools/releases/platform-tools) (`adb` in your system PATH)
- A connected Android device with USB debugging enabled, or an emulator exposing ADB

## Install and launch (Windows PowerShell)

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

If PowerShell blocks activation, use `.venv\Scripts\python.exe -m pip install -r requirements.txt` then `.venv\Scripts\python.exe app.py`.

Confirm device setup with `adb devices`; authorize debugging on the phone when prompted. For emulators, start the emulator before clicking **Refresh**.

## MuMu Player 12 v6.8.3 (Android 12)

1. Launch your Android 12 MuMu instance and leave it running.
2. In MuMu, use **Issues Diagnostic** (or the multi-instance manager's ADB information) to find the exact instance's **ADB debugging port**. The primary MuMu 12 instance often uses `16384`; other instances commonly use `16416`, `16448`, etc. Some MuMu installations use `7555`. **Read the displayed port rather than guessing.**
3. Open the visual editor, click **Connect MuMu**, enter the port, and select `127.0.0.1:PORT` in the Device dropdown.
4. Click **Capture template** to obtain image crops from the emulator itself; your Windows cursor is not moved by ADB screenshots or taps.
5. Press **Run**. The application sends taps and key events directly to Android, so your Windows mouse and keyboard remain usable.

**Important:** MuMu must remain running; minimizing or covering its Windows window generally does not prevent ADB screenshots. Keep the Android resolution/orientation constant for reliable template matching. The **global F8** listener is active while the editor is open, even if another window is focused; STOP works in the editor too. ADB commands have a 12-second timeout, so stopping an in-flight command is not instantaneous. Some games may restrict automation.

## Creating a workflow

1. Choose your ADB device and click **Capture template**. Drag a rectangle around the button/icon you want to recognize; save the crop as a PNG.
2. Select a step on the left. Set its **Action** to `find_image`, choose your PNG, set `threshold` (typically 0.80-0.93), attempts, retry delay, and whether to tap the match. Hit **Apply changes**.
3. Use **+ Add** to create follow-up steps. `If found: step ID` and `If missing: step ID` route to separate steps. `Next step ID` overrides moving to the next row. A blank target defaults to the following row.
4. Use `goto` for unconditional jumps and loops. Use `stop` to end execution. A 10,000-step execution ceiling protects against accidental infinite loops.
5. Click **Save as** to store your workflow, then **Run**. Press **F8 globally** or the **STOP** button to abort. Stop is cooperative: an in-flight ADB command may last up to 12 seconds before the engine can interrupt.

## Actions

| Action | Behavior |
| --- | --- |
| `find_image` | Screenshot, OpenCV matchTemplate, retry, optional tap, branch on found/not found |
| `tap` | Tap at absolute pixel coordinates |
| `wait` | Wait interruptibly |
| `swipe` | Swipe between coordinates for a duration |
| `key` | Android key event, e.g. `KEYCODE_BACK` or `KEYCODE_HOME` |
| `text` | Type basic ASCII via `adb shell input text` (special characters may be interpreted by Android) |
| `goto` | Jump to the `Next step ID` |
| `stop` | Finish workflow |

## Important details

- Template matching requires a screenshot crop at **the same resolution, UI scaling, and orientation** as during execution. Avoid flat/solid areas, animations, and large changing text blocks.
- `region` accepts `x,y,width,height`; leave blank to scan the entire screen. Coordinates are **device screenshot pixel** coordinates.
- `threshold` is normalized similarity (0 to 1). A match is reported when the score meets or exceeds this value.
- `retries` means total screenshot attempts, including the first.
- For `find_image`, an empty `on_true` or `on_false` falls back to `next` or the next row; configure explicit branches to avoid ambiguity.
- JSON paths for template images are relative to the workflow JSON folder, or absolute. The included example references `templates/button.png`, which you create yourself through Capture template.
- ADB commands are executed with subprocess argument arrays, not shell interpolation. Do not automate devices/apps you don't have permission to control.
- This tool does not bypass Android permissions or app protections, and is not a background on-device Android app; it runs on a host computer connected to the device/emulator.

## Project files

- `app.py` — Tkinter workflow editor and template crop tool
- `engine.py` — ADB interface, OpenCV recognition and workflow runner
- `example_workflow.json` — example branching workflow (replace placeholder PNG)
- `requirements.txt` — Python packages
