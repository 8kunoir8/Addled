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
#   .\scripts\deploy_to_install.ps1 -ListExclusions   # print the exclude set
#   .\scripts\deploy_to_install.ps1 -WithSkills  # also copy installed market skills
#
# `-WithSkills` exists because market skills are user data (gitignored), so the
# backend sync leaves them behind by design. Installing a skill for the dev tree
# therefore does NOT put it in the running app, which looks like the install
# silently failed. Passing the flag copies them across; it is opt-in so a normal
# deploy can never overwrite the skills someone else installed.
#
# What is excluded is DERIVED from .gitignore, not maintained here. A hand
# written list drifts: this one shipped without `*.db`, `*.log`, `*.jsonl` and
# `facts.json`, so a deploy quietly copied the developer's memory databases,
# their log and their egress trail over the installed app's own. Anything git
# ignores is history, state or a downloaded cache, and none of it belongs in
# someone else's install. `scripts/check_packaging.py` asserts the installer
# and this script exclude the same set.
#
# The script refuses to finish quietly if settings.json changed, so a future
# exclude that is wrong shows up immediately instead of days later.

[CmdletBinding()]
param(
    [switch]$BackendOnly,
    [switch]$DryRun,
    [switch]$ListExclusions,
    # Copy installed market skills across too. Off by default and deliberately
    # so: `market_skills/` is user data (gitignored), and a deploy that moved
    # skills into someone's install without being asked would be overwriting
    # their choices with the developer's. This exists because installing a
    # skill for the dev tree otherwise leaves the running app without it, and
    # the fix must be something the caller asks for.
    [switch]$WithSkills,
    [string]$InstallRoot = "$env:LOCALAPPDATA\Programs\Addled"
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$resources = Join-Path $InstallRoot 'resources'

# git is what keeps this list honest, so its absence is a failure, not a
# fallback to copying everything.
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    throw "git is required: the deploy's exclude list is derived from .gitignore."
}

function Get-IgnoredPaths {
    # Everything .gitignore excludes under a folder, as repo-relative paths.
    # --directory collapses a fully ignored folder to one entry, so a file
    # added inside it later is covered without touching this script.
    param([string]$Under)
    $out = & git -C $repo ls-files --others --ignored --exclude-standard `
                   --directory -- $Under 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "git could not list ignored paths under $Under"
    }
    return @($out | Where-Object { $_ })
}

$ignored = Get-IgnoredPaths -Under 'backend'
$ignoredDirs = @($ignored | Where-Object { $_ -match '[\\/]$' } |
                 ForEach-Object { Join-Path $repo ($_.TrimEnd('\', '/') -replace '/', '\') })
$ignoredFiles = @($ignored | Where-Object { $_ -notmatch '[\\/]$' } |
                  ForEach-Object { Join-Path $repo ($_ -replace '/', '\') })

# Belt and braces, and things git does not track yet: a .pyc that exists only
# because the backend has run, and the settings guard's subject.
$alwaysFiles = @('*.pyc')
$alwaysDirs = @((Join-Path $repo 'backend\__pycache__'))

$excludeDirs = @($ignoredDirs + $alwaysDirs | Sort-Object -Unique)
$excludeFiles = @($ignoredFiles + $alwaysFiles | Sort-Object -Unique)

if ($ListExclusions) {
    # Consumed by check_packaging.py, which compares this against the patterns
    # in electron-builder.yml.
    $excludeDirs | ForEach-Object { "DIR  $_" }
    $excludeFiles | ForEach-Object { "FILE $_" }
    return
}

if (-not (Test-Path $resources)) {
    throw "No installed app at $InstallRoot (looked for $resources)."
}

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

Write-Host ("Deploying backend ({0} ignored path(s), {1} ignored dir(s), excluded by .gitignore)..." -f $excludeFiles.Count, $excludeDirs.Count) -ForegroundColor Cyan
$excludes = @('/XD') + $excludeDirs + @('/XF') + $excludeFiles
Invoke-Sync -From (Join-Path $repo 'backend') -To (Join-Path $resources 'backend') `
            -ExtraArgs $excludes

if (-not $BackendOnly) {
    # The bot bridges live in `bots/`, NOT under `backend/`, so the sync above
    # never touched them. Editing a bot script and deploying looked like it
    # worked — the backend restarted, the dashboard updated — while the running
    # app still ran the OLD JavaScript. Found when a WhatsApp fix was deployed
    # and the installed copy still had the previous code.
    #
    # Packaged builds run them from `app.asar.unpacked\bots` (an external node
    # cannot read inside app.asar), which is where electron-builder puts them.
    # Both layouts are handled so a dev run and an installed app agree.
    $botsFrom = Join-Path $repo 'bots'
    if (Test-Path $botsFrom) {
        $botTargets = @()
        $unpacked = Join-Path $resources 'app.asar.unpacked\bots'
        if (Test-Path (Split-Path $unpacked -Parent)) { $botTargets += $unpacked }
        $plain = Join-Path $resources 'bots'
        if (Test-Path $plain) { $botTargets += $plain }
        if ($botTargets.Count -eq 0) {
            Write-Host "No bots folder in the install; skipping bot scripts." -ForegroundColor Yellow
        }
        foreach ($botsTo in $botTargets) {
            $jsCount = (Get-ChildItem $botsFrom -Filter '*.js' -File -ErrorAction SilentlyContinue).Count
            Write-Host ("Deploying bot scripts ({0} file(s)) -> {1}..." -f $jsCount, $botsTo) -ForegroundColor Cyan
            # Only the scripts, and only *.js. `node_modules` under bots/ is
            # large and installed by npm; `auth/` holds live WhatsApp pairing
            # credentials, which are user data that must survive a deploy.
            Invoke-Sync -From $botsFrom -To $botsTo `
                        -ExtraArgs (@('/XD', 'node_modules', 'auth', 'shared\auth') +
                                    @('/XF', '*.log', 'package-lock.json'))
        }
    } else {
        Write-Host "No bots folder in the repo; skipping bot scripts." -ForegroundColor Yellow
    }
}

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

# Top-level requirement files. The installer ships these through
# electron-builder's extraResources, so an installed app gets them; the deploy
# did not copy them at all, which meant a dev install diverged from what a user
# receives. That matters once code reads them: `install_hf_deps` resolves
# requirements-vision.txt to install the declared vision set, and without the
# file it silently fell back to a hardcoded list — the exact drift the file
# exists to prevent.
foreach ($reqName in @('requirements.txt', 'requirements-vision.txt')) {
    $reqFrom = Join-Path $repo $reqName
    if (Test-Path $reqFrom) {
        Copy-Item -Path $reqFrom -Destination (Join-Path $resources $reqName) -Force
        Write-Host "Deployed $reqName" -ForegroundColor Cyan
    }
}

if ($WithSkills) {
    # Market skills are gitignored user data, so the backend sync above leaves
    # them behind by design. That is right for a normal deploy and wrong when
    # you have just installed skills and want the running app to have them.
    # Copied WITHOUT /MIR: a skill the user installed in their own app must not
    # be deleted by a developer's deploy.
    $skillsFrom = Join-Path $repo 'backend\memory\market_skills'
    $skillsTo = Join-Path $resources 'backend\memory\market_skills'
    if (Test-Path $skillsFrom) {
        $count = (Get-ChildItem $skillsFrom -Directory -ErrorAction SilentlyContinue).Count
        Write-Host ("Deploying market skills ({0})..." -f $count) -ForegroundColor Cyan
        if (-not $DryRun) { New-Item -ItemType Directory -Force -Path $skillsTo | Out-Null }
        Invoke-Sync -From $skillsFrom -To $skillsTo -ExtraArgs @()
    } else {
        Write-Host "No market skills to deploy." -ForegroundColor Yellow
    }
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

# Without this the script's exit status is whatever robocopy set last (1-7 mean
# success to robocopy and failure to everything else), so a caller that checks
# the exit code reads a good deploy as a broken one.
exit 0
