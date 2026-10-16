$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    # Isolate packaging dependencies from the developer's Python installation.
    if (-not (Test-Path '.venv-build\Scripts\python.exe')) {
        py -3 -m venv .venv-build
        if ($LASTEXITCODE -ne 0) { throw 'Creating the build environment failed.' }
    }
    $buildPython = Join-Path $PSScriptRoot '.venv-build\Scripts\python.exe'
    # Replace the old GUI-enabled OpenCV package in pre-migration build environments.
    $legacyOpenCV = & $buildPython -c "from importlib.metadata import distributions; print(int(any(d.metadata['Name'] == 'opencv-python' for d in distributions())))"
    if ($legacyOpenCV -eq '1') {
        # Both distributions share cv2 files, so reinstall headless after removal.
        & $buildPython -m pip uninstall -y opencv-python opencv-python-headless
        if ($LASTEXITCODE -ne 0) { throw 'Removing old OpenCV failed.' }
    }
    & $buildPython -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw 'Installing build dependencies failed.' }
    & $buildPython build_app.py
    if ($LASTEXITCODE -ne 0) { throw 'Building the executable failed.' }
    Write-Host 'Ready: dist\AndroidVisualAutoclicker\AndroidVisualAutoclicker.exe (distribute the entire folder)'
} finally {
    Pop-Location
}
