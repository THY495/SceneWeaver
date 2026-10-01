param([switch]$Game, [switch]$Studio)
if ($Game -and $Studio) { throw 'Choose Game or Studio, not both.' }
$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
Push-Location $workspace
try {
    $buildPython = Join-Path $workspace '.runtime/windows-p0-build/Scripts/python.exe'
    uv export --frozen --no-dev --no-emit-project --no-hashes --output-file .runtime/windows-p0/backend-locked.txt | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Dependency lock export failed.' }
    uv pip install --python $buildPython -r .runtime/windows-p0/backend-locked.txt
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency install failed.' }
    $webFolder = if ($Game) { 'apps/web/dist-game' } else { 'apps/web/dist' }
    $webBuild = if ($Game) { 'build:game' } else { 'build' }
    & npm run $webBuild --prefix apps/web
    if ($LASTEXITCODE -ne 0) { throw 'Selected frontend build failed.' }
    $backendName = if ($Game) { 'GameBackend' } elseif ($Studio) { 'SceneweaverBackend' } else { 'SceneweaverBackendP0' }
    $webEntry = if ($Game) { 'game.html' } else { 'index.html' }
    if (-not (Test-Path -LiteralPath (Join-Path $workspace "$webFolder/$webEntry"))) { throw 'Build the selected frontend first.' }
    & $buildPython prototypes/windows_p0/build_frozen.py --noconfirm --console --onedir --name $backendName --paths (Join-Path $workspace 'src') --distpath .runtime/windows-p0/dist --workpath ".runtime/windows-p0/build-$backendName" --specpath .runtime/windows-p0 --add-data ((Join-Path $workspace $webFolder) + ";$webFolder") --add-data ((Join-Path $workspace 'src/chatbot/items/presets.v1.json') + ';chatbot/items') --collect-data jieba --recursive-copy-metadata langgraph prototypes/windows_p0/backend_host.py
    if ($LASTEXITCODE -ne 0) { throw 'Backend build failed.' }
    if ($Game) {
        & $buildPython scripts/write_game_runtime_capabilities.py .runtime/windows-p0/dist/GameBackend/sceneweaver-runtime-capabilities.json
        if ($LASTEXITCODE -ne 0) { throw 'Game runtime capability recording failed.' }
    }
} finally {
    Pop-Location
}
