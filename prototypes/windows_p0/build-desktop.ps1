param([switch]$Game, [switch]$Studio)
if ($Game -and $Studio) { throw 'Choose Game or Studio, not both.' }
$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
Push-Location $workspace
try {
    $buildPython = Join-Path $workspace '.runtime/windows-p0-build/Scripts/python.exe'
    uv pip install --python $buildPython -r prototypes/windows_p0/requirements-desktop-build.txt
    if ($LASTEXITCODE -ne 0) { throw 'Desktop dependency install failed.' }
    $desktopName = if ($Game) { 'Game' } elseif ($Studio) { 'Sceneweaver' } else { 'SceneweaverDesktopP0' }
    $entry = if ($Game) { 'prototypes/windows_p0/game_launcher.py' } elseif ($Studio) { 'prototypes/windows_p0/studio_launcher.py' } else { 'prototypes/windows_p0/desktop_host.py' }
    & $buildPython prototypes/windows_p0/build_frozen.py --noconfirm --windowed --onedir --paths (Join-Path $workspace 'src') --name $desktopName --distpath .runtime/windows-p0/dist --workpath ".runtime/windows-p0/build-$desktopName" --specpath .runtime/windows-p0 $entry
    if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed.' }
    # This runtime is copied into independent games; carry the code license.
    Copy-Item -LiteralPath (Join-Path $workspace 'LICENSE') -Destination (Join-Path $workspace ".runtime/windows-p0/dist/$desktopName/LICENSE") -Force
} finally {
    Pop-Location
}
