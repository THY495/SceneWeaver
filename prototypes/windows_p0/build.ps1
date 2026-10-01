$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
Push-Location $workspace
try {
    $buildPython = Join-Path $workspace '.runtime/windows-p0-build/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $buildPython)) {
        throw 'Create the isolated build environment from requirements-build.txt first.'
    }
    $source = Join-Path $workspace 'src'
    $presets = (Join-Path $source 'chatbot/items/presets.v1.json') + ';chatbot/items'
    & $buildPython -m PyInstaller --noconfirm --windowed --onedir --name SceneweaverP0 --paths $source --distpath .runtime/windows-p0/dist --workpath .runtime/windows-p0/build --specpath .runtime/windows-p0 --add-data $presets prototypes/windows_p0/probe.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    & $buildPython prototypes/windows_p0/notices.py .runtime/windows-p0/dist/SceneweaverP0
    if ($LASTEXITCODE -ne 0) { throw 'Dependency notice collection failed.' }
} finally {
    Pop-Location
}
