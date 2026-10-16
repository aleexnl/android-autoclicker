$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    # Isolate packaging dependencies from the developer's Python installation.
    if (-not (Test-Path '.venv-build\Scripts\python.exe')) {
        py -3 -m venv .venv-build
        if ($LASTEXITCODE -ne 0) { throw 'Creating the build environment failed.' }
    }
    $buildPython = Join-Path $PSScriptRoot '.venv-build\Scripts\python.exe'
    & $buildPython -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw 'Installing build dependencies failed.' }
    & $buildPython -m PyInstaller --noconfirm --clean --onefile --windowed --noupx `
        --name AndroidVisualAutoclicker --specpath build `
        --collect-data sv_ttk `
        --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 app.py
    if ($LASTEXITCODE -ne 0) { throw 'Building the executable failed.' }
    Write-Host 'Ready: dist\AndroidVisualAutoclicker.exe'
} finally {
    Pop-Location
}
