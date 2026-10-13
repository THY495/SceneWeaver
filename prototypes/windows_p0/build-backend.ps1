param([switch]$Game, [switch]$Studio, [string]$OutputRoot)
if ($Game -and $Studio) { throw 'Choose Game or Studio, not both.' }
$ErrorActionPreference = 'Stop'
$target = if ($Game) { 'GameBackend' } elseif ($Studio) { 'SceneweaverBackend' } else { 'SceneweaverBackendP0' }
$builder = Join-Path $PSScriptRoot 'native_build.py'
$arguments = @('run', '--no-project', '--no-config', '--no-env-file', '--python', '3.13.9', 'python', $builder, $target)
if ($OutputRoot) { $arguments += @('--output', $OutputRoot) }
& uv @arguments
if ($LASTEXITCODE -ne 0) { throw 'Backend build failed; inspect the output receipts.' }
