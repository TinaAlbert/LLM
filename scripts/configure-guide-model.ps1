# Run locally in an interactive PowerShell terminal. No network request; no key output.
$llmProjectRoot = Split-Path -Parent $PSScriptRoot
$llmConfigPath = Join-Path $llmProjectRoot '.local\guide-model.json'
if (Test-Path -LiteralPath $llmConfigPath) { throw 'Existing guide-model.json preserved. Edit it locally after reviewing its current settings.' }
New-Item -ItemType Directory -Path (Split-Path -Parent $llmConfigPath) -Force | Out-Null
$llmSecret = Read-Host 'DeepSeek API Key (hidden input; stored only in ignored local configuration)' -AsSecureString
$llmPointer = [IntPtr]::Zero
try {
    $llmPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($llmSecret)
    $llmPlainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($llmPointer)
    if ($llmPlainKey.Length -lt 16 -or $llmPlainKey.Length -gt 512 -or $llmPlainKey -match '\s') { throw 'Invalid key shape; nothing saved.' }
    $llmConfig = [ordered]@{
        package='DS-1007';service='deepseek-official';model='deepseek-flash';model_revision='DeepSeek-V4.1-Flash'
        account_ref='existing-deepseek';api_key=$llmPlainKey
        account_review=[ordered]@{
            checked_on='';existing_account=$true;balance_available=$true;model_access=$false;evidence_ref=''
        }
        price_review=[ordered]@{
            checked_on='';input_per_million='2';cache_hit_per_million='0.04';output_per_million='8';additional_fee_upper_cny=$null;evidence_ref=''
        }
    }
    # CreateNew also protects against another local process creating the file during the prompt.
    $llmStream = [IO.File]::Open($llmConfigPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try {
        $llmBytes = [Text.UTF8Encoding]::new($false).GetBytes(($llmConfig | ConvertTo-Json -Depth 4))
        $llmStream.Write($llmBytes,0,$llmBytes.Length)
        $llmStream.Flush($true)
        [Array]::Clear($llmBytes,0,$llmBytes.Length)
    } finally { $llmStream.Dispose() }
    Write-Output "Saved: $llmConfigPath"
    Write-Output 'No API request made. Model access, current account pricing/fees, token bound and full isolation still require verification.'
} finally {
    if ($llmPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($llmPointer) }
    $llmPlainKey=$null;$llmConfig=$null
    $llmSecret.Dispose()
}
