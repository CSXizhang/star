# Shared checks for player packages. No game or global client changes here.
function Get-ReleaseHash([string]$Path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '') }
    finally { $stream.Dispose(); $sha.Dispose() }
}

function Get-ReleasePath([string]$Root, [string]$Relative) {
    $base = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if ([IO.Path]::IsPathRooted($Relative) -or $Relative -match '(^|[\\/])\.\.([\\/]|$)|:') { throw "Unsafe release path: $Relative" }
    if ($Relative -match '^(?i)(data|config|\.kimi-code|\.env|\.env.local)([\\/]|$)') { throw 'Release manifest cannot own player data or settings.' }
    $path = [IO.Path]::GetFullPath((Join-Path $base $Relative))
    if (-not $path.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) { throw 'Release path escapes package.' }
    $probe = Split-Path -Parent $path
    while ($probe.Length -ge $base.TrimEnd('\').Length) {
        if ((Test-Path -LiteralPath $probe) -and ((Get-Item -LiteralPath $probe -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Linked release directory not allowed: $probe" }
        $probe = Split-Path -Parent $probe
    }
    return $path
}

function Read-VerifiedRelease([string]$Root, [switch]$FullVerify) {
    $manifest = Join-Path $Root 'release-manifest.json'
    $package = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($package.manifestType -ne 'windows-release' -or $package.schemaVersion -ne 1 -or
        $package.platform -ne 'windows-x64' -or $package.python -ne 'runtime/python/python.exe') { throw 'Unsupported release package.' }
    $seen = @{}
    $requiredFiles = @('manifest.json', 'stardewai.companion.mod.dll', 'runtime/python/python.exe', 'runtime/src/stardew_ai_runtime/chat_bridge.py', 'tools/start-companion.ps1')
    foreach ($entry in $package.files) {
        $key = $entry.path.Replace('\', '/').ToLowerInvariant()
        if ($seen.ContainsKey($key)) { throw "Duplicate release file: $key" }
        $seen[$key] = $entry.sha256
        if (-not $FullVerify -and $key -notin $requiredFiles) { continue }
        $path = Get-ReleasePath $Root $entry.path
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Release file missing: $($entry.path)" }
        if ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked release file not allowed.' }
        if ((Get-ReleaseHash $path) -ne $entry.sha256) { throw "Release file changed: $($entry.path). Reinstall the matching package." }
    }
    foreach ($required in $requiredFiles) {
        if (-not $seen.ContainsKey($required)) { throw "Required release file omitted: $required" }
    }
    $native = Get-Content -LiteralPath (Join-Path $Root 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($native.Version -ne $package.version -or $native.EntryDll -ne 'StardewAI.Companion.Mod.dll' -or
        $seen['stardewai.companion.mod.dll'] -ne $package.modSha256) { throw 'Mod/runtime release pairing differs.' }
    return $package
}

function Start-ReleaseCompanion([string]$Root, [switch]$CheckOnly, [string[]]$ForwardArgs) {
    $package = Read-VerifiedRelease $Root -FullVerify:$CheckOnly
    $python = Get-ReleasePath $Root $package.python
    if ($CheckOnly) {
        & $python -B -c "import sqlite3,ssl,mcp,win32job; import stardew_ai_runtime.chat_bridge; print('Portable runtime imports OK')"
        if ($LASTEXITCODE -ne 0) { throw 'Packaged Python could not import runtime dependencies.' }
        Write-Host "Release $($package.version) verified. No service or game started."
        return
    }
    if ($ForwardArgs | Where-Object { $_ -match '^--run-dir(=|$)' }) { throw 'Release is bound to its own Mod directory.' }
    $configPath = Join-Path $Root 'config/chat-backend.json'
    if (-not (Test-Path -LiteralPath $configPath)) { throw 'Run the setup entry first to select your AI backend and model.' }
    $settings = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($settings.backend -notin @('kimi', 'agy')) { throw 'Choose Kimi or agy in setup first.' }
    if (-not (Get-Command ($settings.backend + '.exe') -ErrorAction SilentlyContinue)) { throw "AI CLI $($settings.backend) is missing. Install and sign in to that CLI, then reopen the game." }
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $key = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes([IO.Path]::GetFullPath($Root).ToLowerInvariant()))).Replace('-', '') }
    finally { $hash.Dispose() }
    $mutex = New-Object Threading.Mutex($false, "Local\StardewAI.Companion.$key")
    $acquired = $false
    try {
        try { $acquired = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired = $true }
        if (-not $acquired) { Write-Host 'Companion service already running for this Mod.'; return }
        Push-Location -LiteralPath $Root
        try {
            $env:PYTHONUTF8 = '1'
            $env:PYTHONDONTWRITEBYTECODE = '1'
            & $python -B -m stardew_ai_runtime.chat_bridge --run-dir $Root @ForwardArgs
            if ($LASTEXITCODE -ne 0) { throw "Companion service exited with code $LASTEXITCODE." }
        } finally { Pop-Location }
    } finally {
        if ($acquired) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}
