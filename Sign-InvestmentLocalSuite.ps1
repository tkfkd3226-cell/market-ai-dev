[CmdletBinding()]
param(
    [string]$FilePath = "",
    [string]$PublisherName = "Investment Local Suite",
    [string]$TimestampServer = "http://timestamp.digicert.com"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $FilePath) {
    $FilePath = Join-Path $Root "InvestmentLocalSuite.exe"
}
$FilePath = [IO.Path]::GetFullPath($FilePath)
$Subject = "CN=$PublisherName"

if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
    throw "File not found: $FilePath"
}

$cert = @(
    Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Subject -eq $Subject -and
        $_.HasPrivateKey -and
        $_.NotAfter -gt (Get-Date)
    } |
    Sort-Object NotAfter -Descending
) | Select-Object -First 1

if (-not $cert) {
    throw @"
Code-signing certificate not found: $Subject
Run Initialize-InvestmentLocalSuiteSigning.ps1 once from the market-ai-dev folder.
That development helper creates/trusts the local signing certificate for this Windows user only.
"@
}

$signed = $null
if ($TimestampServer) {
    try {
        $signed = Set-AuthenticodeSignature `
            -FilePath $FilePath `
            -Certificate $cert `
            -HashAlgorithm SHA256 `
            -TimestampServer $TimestampServer
    } catch {
        Write-Host "[SIGN] Timestamp unavailable; retrying without timestamp." -ForegroundColor Yellow
    }
}

if (-not $signed -or $signed.Status -ne "Valid") {
    $signed = Set-AuthenticodeSignature `
        -FilePath $FilePath `
        -Certificate $cert `
        -HashAlgorithm SHA256
}

$verify = Get-AuthenticodeSignature -FilePath $FilePath
if ($verify.Status -ne "Valid") {
    throw "Signature verification failed: $($verify.Status) / $($verify.StatusMessage)"
}

Write-Host "[SIGN] PASS"
Write-Host "[SIGN] File       : $FilePath"
Write-Host "[SIGN] Publisher  : $($verify.SignerCertificate.Subject)"
Write-Host "[SIGN] Thumbprint : $($verify.SignerCertificate.Thumbprint)"
