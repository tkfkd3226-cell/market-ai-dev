[CmdletBinding()]
param(
    [string]$PublisherName = "Investment Local Suite"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Subject = "CN=$PublisherName"
$Now = Get-Date

function Write-Step([string]$Message) {
    Write-Host "[SIGN-INIT] $Message"
}

$existing = @(
    Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Subject -eq $Subject -and
        $_.HasPrivateKey -and
        $_.NotAfter -gt $Now
    } |
    Sort-Object NotAfter -Descending
) | Select-Object -First 1

if ($existing) {
    $cert = $existing
    Write-Step "Existing code-signing certificate found: $($cert.Thumbprint)"
} else {
    Write-Step "Creating CurrentUser code-signing certificate: $Subject"
    $cert = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject $Subject `
        -CertStoreLocation "Cert:\CurrentUser\My" `
        -KeyAlgorithm RSA `
        -KeyLength 3072 `
        -HashAlgorithm SHA256 `
        -KeyExportPolicy NonExportable `
        -NotAfter $Now.AddYears(5)
}

$tempCer = Join-Path $env:TEMP ("InvestmentLocalSuite-signing-" + [Guid]::NewGuid().ToString("N") + ".cer")
try {
    Export-Certificate -Cert $cert -FilePath $tempCer -Force | Out-Null

    $trustedRoot = @(
        Get-ChildItem Cert:\CurrentUser\Root -ErrorAction SilentlyContinue |
        Where-Object { $_.Thumbprint -eq $cert.Thumbprint }
    ) | Select-Object -First 1
    if (-not $trustedRoot) {
        Write-Step "Trusting certificate in CurrentUser\\Root."
        Import-Certificate -FilePath $tempCer -CertStoreLocation "Cert:\CurrentUser\Root" | Out-Null
    }

    $trustedPublisher = @(
        Get-ChildItem Cert:\CurrentUser\TrustedPublisher -ErrorAction SilentlyContinue |
        Where-Object { $_.Thumbprint -eq $cert.Thumbprint }
    ) | Select-Object -First 1
    if (-not $trustedPublisher) {
        Write-Step "Trusting certificate in CurrentUser\\TrustedPublisher."
        Import-Certificate -FilePath $tempCer -CertStoreLocation "Cert:\CurrentUser\TrustedPublisher" | Out-Null
    }
} finally {
    Remove-Item -LiteralPath $tempCer -Force -ErrorAction SilentlyContinue
}

$verify = @(
    Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Subject -eq $Subject -and
        $_.Thumbprint -eq $cert.Thumbprint -and
        $_.HasPrivateKey
    }
) | Select-Object -First 1

if (-not $verify) {
    throw "Code-signing certificate provisioning failed: $Subject"
}

Write-Host "[SIGN-INIT] PASS"
Write-Host "[SIGN-INIT] Subject    : $($verify.Subject)"
Write-Host "[SIGN-INIT] Thumbprint : $($verify.Thumbprint)"
Write-Host "[SIGN-INIT] Expires    : $($verify.NotAfter.ToString('yyyy-MM-dd'))"
Write-Host "[SIGN-INIT] Scope      : CurrentUser only"
