# Dot-source from PowerShell; changes this session only.
$llmRoot = Split-Path -Parent $PSScriptRoot
$llmTools = Join-Path $llmRoot '.local\reinstall\tools'
$llmPaths = @(
    (Join-Path $llmRoot '.venv\Scripts'),
    (Join-Path $llmTools 'python'),
    (Join-Path $llmTools 'python\DLLs'),
    (Join-Path $llmTools 'node'),
    (Join-Path $llmTools 'git\cmd'),
    (Join-Path $llmTools 'mingw64\bin')
)
foreach ($llmPath in $llmPaths) {
    if (-not (Test-Path -LiteralPath $llmPath -PathType Container)) {
        throw "Missing project tool directory: $llmPath"
    }
}
$llmCertificate = Join-Path $llmTools 'git\mingw64\etc\ssl\certs\ca-bundle.crt'
if (-not (Test-Path -LiteralPath $llmCertificate -PathType Leaf)) { throw 'Missing Git certificate bundle' }
$env:PATH = ($llmPaths -join ';') + ';' + $env:PATH
$env:PYTHONUTF8 = '1'
# Override the saved Git copy's historical F: certificate path without modifying its backup.
$env:GIT_SSL_CAINFO = $llmCertificate
$env:NPM_CONFIG_CACHE = Join-Path $llmRoot '.local\npm-cache'
$env:NPM_CONFIG_USERCONFIG = Join-Path $llmRoot '.local\npm-user-empty.conf'
$env:NPM_CONFIG_GLOBALCONFIG = Join-Path $llmRoot '.local\npm-global-empty.conf'
Write-Output 'Project tools selected for this PowerShell session.'
