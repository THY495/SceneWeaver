param(
    [Parameter(Mandatory=$true)][string]$Root,
    [Parameter(Mandatory=$true)][string]$Report
)
$ErrorActionPreference = 'Stop'
$releaseRoot = (Resolve-Path -LiteralPath $Root).Path
$walkRoot = [string]::Concat([char]92, [char]92, '?', [char]92, $releaseRoot)
if (-not (Test-Path -LiteralPath $releaseRoot -PathType Container)) {
    throw 'Root must be an existing release directory.'
}
if (Test-Path -LiteralPath $Report) { throw 'Use a new report path.' }
# Read-only inventory. This neither signs files nor changes trust policy.
$rows = @(Get-ChildItem -LiteralPath $walkRoot -Recurse -File |
    Where-Object { $_.Extension -in @('.exe', '.dll', '.pyd') } |
    ForEach-Object {
        # Authenticode and Get-FileHash can fail on an existing path over MAX_PATH.
        $longPath = $_.FullName
        $signature = Get-AuthenticodeSignature -LiteralPath $longPath
        [ordered]@{
            path = $_.FullName.Substring($walkRoot.Length).TrimStart([char[]]@('\', '/'))
            sha256 = (Get-FileHash -LiteralPath $longPath -Algorithm SHA256).Hash
            status = [string]$signature.Status
            signer = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { $null }
            signer_key_oid = if ($signature.SignerCertificate) { $signature.SignerCertificate.PublicKey.Oid.Value } else { $null }
            timestamped = [bool]$signature.TimeStamperCertificate
        }
    })
$result = [ordered]@{
    scope = 'Authenticode inventory only; not Smart App Control or clean-machine acceptance'
    root = $releaseRoot
    recorded_at = (Get-Date).ToUniversalTime().ToString('o')
    total = $rows.Count
    not_valid = @($rows | Where-Object { $_.status -ne 'Valid' }).Count
    files = $rows
}
$result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Report -Encoding utf8
Write-Output "Inventoried $($rows.Count) binaries; $($result.not_valid) do not have a Valid signature."
