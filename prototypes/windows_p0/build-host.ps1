$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
Push-Location $workspace
try {
    $buildPython = Join-Path $workspace '.runtime/windows-p0-build/Scripts/python.exe'
    & $buildPython -m PyInstaller --noconfirm --console --onedir --name SceneweaverHostP0 --distpath .runtime/windows-p0/dist --workpath .runtime/windows-p0/build-host --specpath .runtime/windows-p0 prototypes/windows_p0/managed_host.py
    if ($LASTEXITCODE -ne 0) { throw 'Host build failed.' }
} finally {
    Pop-Location
}
