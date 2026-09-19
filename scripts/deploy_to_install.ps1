# Deploy this build into the installed Addled, without touching the user's data.
#
# Why this exists: deploying `backend/` wholesale over the install is the
# obvious thing to do and it is destructive. `backend/memory/` holds both code
# and the user's data, so a plain `robocopy backend resources\backend /E`
# overwrites settings.json, the conversations, the memory, the goals and the
# forged skills with the developer's copies.
#
# That happened, and it was not obvious afterwards. Robocopy preserves
# timestamps, so the clobbered settings.json was *older* than the change that had
# just been made to it - which reads as "something restored an old file from
# inside the app" rather than "the deploy overwrote it". The visible symptoms
# were a provider choice that reverted on restart, a remote port and password
# that changed, and provider API keys that disappeared, because the developer's
# config had none.
#
# Usage:
#   .\scripts\deploy_to_install.ps1              # deploy backend + dashboard
#   .\scripts\deploy_to_install.ps1 -BackendOnly
#   .\scripts\deploy_to_install.ps1 -DryRun      # list what would move
#
# The script refuses to finish quietly if settings.json changed, so a future
# exclude that is wrong shows up immediately instead of days later.

[CmdletBinding()]
param(
    [switch]$BackendOnly,
    [switch]$DryRun,
    [string]$InstallRoot = "$env:LOCALAPPDATA\Programs\Addled"
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$resources = Join-Path $InstallRoot 'resources'

if (-not (Test-Path $resources)) {
    throw "No installed app at $InstallRoot (looked for $resources)."
}

# User data. Every one of these lives beside code inside backend/memory/, which
# is exactly why a directory-level exclude cannot be used.
$dataDirs = @(
    'goals', 'journal', 'market_skills', 'models', 'skins', 'integrations',
    'guidelines', 'forged_skills', 'wiki', 'sop', 'snapshots', 'projects',
    'conversations', 'logs'
) | ForEach-Object { Join-Path $repo "backend\memory\$_" }

$dataFiles = @(
    'settings.json', 'chat_history.json', 'user_profile.json',
    'models_catalog.json', 'maintenance_state.json', 'links.db',
    'links.db-wal', 'links.db-shm', 'session_context.json'
)

$settings = Join-Path $resources 'backend\memory\settings.json'
$beforeHash = if (Test-Path $settings) { (Get-FileHash $settings).Hash } else { '' }
$beforeJson = if (Test-Path $settings) {
    Get-Content $settings -Raw | ConvertFrom-Json
} else { $null }

function Invoke-Sync {
    param([string]$From, [string]$To, [string[]]$ExtraArgs)
    # Not $args: that is a PowerShell automatic variable.
    $robocopyArgs = @($From, $To, '/E', '/NFL', '/NDL', '/NJH', '/NP') + $ExtraArgs
    if ($DryRun) { $robocopyArgs += '/L' }
    & robocopy @robocopyArgs | Out-Null
    $code = $LASTEXITCODE
    # 0-7 are success for robocopy; 8 and above are failures.
    if ($code -ge 8) { throw "robocopy $From -> $To failed with exit code $code" }
}

Write-Host "Deploying backend (user data excluded)..." -ForegroundColor Cyan
$excludes = @('/XD') + ($dataDirs + @((Join-Path $repo 'backend\__pycache__'))) +
            @('/XF') + ($dataFiles + @('*.pyc'))
Invoke-Sync -From (Join-Path $repo 'backend') -To (Join-Path $resources 'backend') `
            -ExtraArgs $excludes

if (-not $BackendOnly) {
    $out = Join-Path $repo 'dashboard\out'
    if (-not (Test-Path (Join-Path $out 'index.html'))) {
        throw "dashboard\out is not built. Run: cd dashboard; npm run build"
    }
    Write-Host "Deploying dashboard (replaced wholesale)..." -ForegroundColor Cyan
    $dash = Join-Path $resources 'dashboard'
    if (-not $DryRun -and (Test-Path $dash)) { Remove-Item "$dash\*" -Recurse -Force }
    Invoke-Sync -From $out -To $dash -ExtraArgs @()
}

if ($DryRun) {
    Write-Host "Dry run - nothing was written." -ForegroundColor Yellow
    return
}

# The guard that would have caught the original mistake.
if (Test-Path $settings) {
    $afterHash = (Get-FileHash $settings).Hash
    if ($beforeHash -and $afterHash -ne $beforeHash) {
        Write-Host "WARNING: settings.json changed during the deploy." -ForegroundColor Red
        Write-Host "Something in the exclude list is wrong - the user's settings" -ForegroundColor Red
        Write-Host "have been overwritten. Restore them before continuing." -ForegroundColor Red
        exit 1
    }
    $afterJson = Get-Content $settings -Raw | ConvertFrom-Json
    Write-Host ("settings.json untouched: active provider = '{0}', " -f $afterJson.providers.active) -ForegroundColor Green
    if ($beforeJson -and $beforeJson.providers.active -ne $afterJson.providers.active) {
        Write-Host "  (the provider choice changed - investigate)" -ForegroundColor Red
        exit 1
    }
}

Write-Host "Deployed. A running Addled needs a restart to load new backend code." -ForegroundColor Yellow
Write-Host "  Kill the backend process; the Electron shell respawns it in ~3s." -ForegroundColor Yellow
