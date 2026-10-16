# Android Visual Autoclicker

A desktop **Python/PySide6 (Qt Widgets)** workflow editor for controlling an Android phone or emulator through ADB. It supports OpenCV screenshot matching, branching, retries, taps, swipes, key presses, text input, loops, JSON workflows, and emergency stop.

The editor uses Qt's platform style, native file dialogs, keyboard shortcuts, and resizable panels. **System**, **Light**, and **Dark** appearance use Qt's OS color-scheme integration; explicit overrides depend on the platform/style. Device discovery, connection, screenshot capture, and workflow execution run off the GUI thread. Prepared image templates are cached for each run, and the execution log is bounded to 2,000 lines.

## Run from source (Windows, Linux, macOS)

Use Python 3.10+ and the Android SDK [Platform Tools](https://developer.android.com/tools/releases/platform-tools) for your OS. Put `adb` on PATH or place the complete `platform-tools` directory beside `app.py`. Connect a device with USB debugging enabled and authorize it when prompted.

Windows PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

Linux/macOS:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

Use a desktop OS/version supported by the installed Qt release. Linux needs the system libraries for Qt's X11 or Wayland platform plugin; USB devices may need appropriate udev rules. No Tkinter installation is required. When upgrading an existing environment, uninstall `opencv-python` before installing the new requirements: the app now uses `opencv-python-headless` to avoid a second GUI toolkit. Do not install both OpenCV packages in the same environment.

## Packaged application

Build separately on each target OS and architecture. Windows builds cannot produce macOS or Linux binaries.

```sh
python -m pip install -r requirements-build.txt
python build_app.py
```

On Windows, `build_windows.ps1` creates/uses `.venv-build`, installs dependencies, and invokes the same builder:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_windows.ps1
```

Outputs:

- Windows: `dist/AndroidVisualAutoclicker/AndroidVisualAutoclicker.exe`
- Linux: `dist/AndroidVisualAutoclicker/AndroidVisualAutoclicker`
- macOS: `dist/AndroidVisualAutoclicker.app`

**Distribute the entire application folder or `.app` bundle.** The directory build avoids extracting Python/Qt libraries on every launch. Old `dist/AndroidVisualAutoclicker.exe` single-file builds are not updated by this script. macOS public distribution still requires signing/notarization with your own credentials.

Place the OS-specific `platform-tools` folder beside the executable (beside the `.app` bundle on macOS), or install ADB on PATH. On Unix systems the `adb` binary must be executable. Windows requires the ADB DLLs from the complete Platform Tools distribution.

## Settings and workflows

The default workflow (`workflow.json`), captured templates (`templates/`), and appearance (`settings.json`) are stored in:

- Windows: `%LOCALAPPDATA%/AndroidVisualAutoclicker`
- macOS: `~/Library/Application Support/AndroidVisualAutoclicker`
- Linux: `$XDG_CONFIG_HOME/AndroidVisualAutoclicker`, or `~/.config/AndroidVisualAutoclicker`

Existing workflow JSON files remain supported: use **Open** to select them, including old `example_workflow.json` files. Relative template paths resolve against the workflow's directory. Keep templates with the workflow when moving it between computers; absolute paths need updating on a different computer. Choose **Apply changes** before selecting a different step. Save and Run apply the currently selected step automatically.

## Keyboard shortcuts and stop behavior

- F6: run; F8 or Escape: stop while the app is focused.
- Ctrl+O / Ctrl+S / Ctrl+Shift+S (Command on macOS): open / save / save as.
- Global F8 uses `pynput` where available. macOS may require Accessibility/Input Monitoring permission. Under Wayland, the app uses focused F8 and the STOP button instead of claiming a system-wide shortcut. A failed global listener does not prevent startup.

Stop is cooperative: an in-flight ADB command may take up to its 12-second timeout. Closing waits for background operations to finish; a connect-and-refresh operation can involve two such commands. A stop received during screenshot capture is checked before sending a matched tap. Editing and device operations are disabled while a workflow runs.

## Verification

```sh
python -m unittest discover -s tests -v
python scripts/smoke_package.py  # After building: initialize the packaged Qt app and exit
```

Tests use fake ADB devices and Qt's offscreen platform to cover workflow compatibility, matching, caching, crop coordinates, background responsiveness, stop behavior, and shutdown. They do not replace real-device testing or native OS packaging checks. The CI matrix builds and runs tests on Windows, Linux, and macOS; Linux/macOS runtime compatibility must still be verified on target desktops.

## MuMu Player 12 v6.8.3 (Android 12)

1. Launch your Android 12 MuMu instance and leave it running.
2. In MuMu, use **Issues Diagnostic** (or the multi-instance manager's ADB information) to find the exact instance's **ADB debugging port**. The primary MuMu 12 instance often uses `16384`; other instances commonly use `16416`, `16448`, etc. Some MuMu installations use `7555`. **Read the displayed port rather than guessing.**
3. Open the visual editor, click **Connect MuMu**, enter the port, and select `127.0.0.1:PORT` in the Device dropdown.
4. Click **Capture template** to obtain image crops from the emulator itself; your Windows cursor is not moved by ADB screenshots or taps.
5. Press **Run**. The application sends taps and key events directly to Android, so your Windows mouse and keyboard remain usable.

**Important:** MuMu must remain running; minimizing or covering its Windows window generally does not prevent ADB screenshots. Keep the Android resolution/orientation constant for reliable template matching. The **global F8** listener is available where the OS permits it; see the shortcut limitations above. STOP works in the editor too. ADB commands have a 12-second timeout, so stopping an in-flight command is not instantaneous. Some games may restrict automation.

## Creating a workflow

1. Choose your ADB device and click **Capture template**. Drag a rectangle around the button/icon you want to recognize; save the crop as a PNG.
2. Select a step on the left. Set its **Action** to `find_image`, choose your PNG, set `threshold` (typically 0.80-0.93), attempts, retry delay, and whether to tap the match. Hit **Apply changes**.
3. Use **+ Add** to create follow-up steps. `If found: step ID` and `If missing: step ID` route to separate steps. `Next step ID` overrides moving to the next row. A blank target defaults to the following row.
4. Use `goto` for unconditional jumps and loops. Use `stop` to end execution. A 10,000-step execution ceiling protects against accidental infinite loops.
5. Click **Save as** to store your workflow, then **Run**. Press **F8** (globally where available) or the **STOP** button to abort. Stop is cooperative: an in-flight ADB command may last up to 12 seconds before the engine can interrupt.

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

- `app.py` — Qt workflow editor, background device jobs, and template crop tool
- `engine.py` — ADB interface, OpenCV recognition and workflow runner
- `example_workflow.json` — example branching workflow (replace placeholder PNG)
- `requirements.txt` — Python packages

- `workflow_model.py` — toolkit-independent editor fields and defaults
- `build_app.py` — platform-aware PyInstaller directory/app-bundle build
- `tests/test_migration.py` — engine and Qt migration regression tests
