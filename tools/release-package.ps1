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
    $requiredFiles += @('docs/agent-core-instructions-draft.md', 'docs/agent-domain-guidance-draft.md', 'agent-skills/stardew-companion/SKILL.md')
    $requiredFiles += 'agent-skills/stardew-companion/references/ingame-execution.md'
    foreach ($topic in @('crop-selection','farm-region','livestock-processing','procurement-travel','wiki-lookup','daily-rhythm')) { $requiredFiles += "docs/agent-guidance/$topic.md" }
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

function Write-ReleaseStartupDiagnostic([string]$Root, [System.Collections.IDictionary]$Details) {
    if (-not $Root) { return }
    try {
        $directory = Join-Path $Root 'data'
        [void][IO.Directory]::CreateDirectory($directory)
        # Callers supply only installation paths and known startup states, never
        # credentials, the full environment, CLI output, or player messages.
        $record = [ordered]@{ time = (Get-Date -Format o) }
        foreach ($key in $Details.Keys) { $record[$key] = $Details[$key] }
        Add-Content -LiteralPath (Join-Path $directory 'release-start.log') -Value ($record | ConvertTo-Json -Compress) -Encoding UTF8
    } catch {
        # Diagnostic logging must not prevent an otherwise valid launch.
    }
}

function Find-ReleaseCachedNpmCli([string]$LocalAppData, [string]$Backend, [string]$LogRoot = '') {
    if (-not $LocalAppData) { return $null }
    # npm installed from the packaged Codex app can live in its redirected
    # Roaming directory. Steam sees the physical cache, not that app's alias.
    $packages = Join-Path $LocalAppData 'Packages'
    if (-not (Test-Path -LiteralPath $packages -PathType Container)) { return $null }
    $codexPackages = Get-ChildItem -LiteralPath $packages -Directory -Filter 'OpenAI.Codex_*' -ErrorAction SilentlyContinue | Sort-Object Name
    foreach ($package in $codexPackages) {
        $npmCli = Join-Path $package.FullName ("LocalCache\Roaming\npm\$Backend.cmd")
        $exists = Test-Path -LiteralPath $npmCli -PathType Leaf
        Write-ReleaseStartupDiagnostic $LogRoot @{ event = 'cli-probe'; backend = $Backend; path = $npmCli; exists = $exists; source = 'codex-app-cache' }
        if ($exists) { return $npmCli }
    }
    return $null
}

function Find-ReleaseCachedDshCli([string]$LocalAppData, [string]$LogRoot = '') {
    return Find-ReleaseCachedNpmCli $LocalAppData 'dsh' -LogRoot $LogRoot
}

function Find-ReleaseAiCli([string]$Backend, [string]$LogRoot = '') {
    # Steam inherits its own PATH. Resolve the current user's installation too,
    # without modifying the machine/user PATH or the selected model settings.
    $application = Get-Command ($Backend + '.exe') -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    # npm exposes dsh and mcode through .cmd shims on Windows. The prefix need not be
    # APPDATA/npm, so honor a PATH installation before probing known folders.
    if (-not $application -and $Backend -in @('dsh', 'mcode')) {
        $application = Get-Command ($Backend + '.cmd') -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if ($application) {
        Write-ReleaseStartupDiagnostic $LogRoot @{ event = 'cli-resolved'; backend = $Backend; cli = $application.Source; source = 'PATH' }
        return $application.Source
    }
    if ($Backend -eq 'codex' -and $env:LOCALAPPDATA) {
        $desktopBin = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
        if (Test-Path -LiteralPath $desktopBin -PathType Container) {
            $desktopCli = Get-ChildItem -LiteralPath $desktopBin -Directory | ForEach-Object {
                $binary = Join-Path $_.FullName 'codex.exe'
                if (Test-Path -LiteralPath $binary -PathType Leaf) { Get-Item -LiteralPath $binary }
            } | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
            if ($desktopCli) {
                Write-ReleaseStartupDiagnostic $LogRoot @{ event = 'cli-resolved'; backend = $Backend; cli = $desktopCli.FullName; source = 'desktop' }
                return $desktopCli.FullName
            }
        }
    }
    if ($Backend -in @('dsh', 'mcode')) {
        # A game/test launcher may override APPDATA. Windows' actual user folder
        # is an independent fallback; do not change the game's APPDATA itself.
        $roamingFolders = @($env:APPDATA, [Environment]::GetFolderPath([Environment+SpecialFolder]::ApplicationData))
        foreach ($folder in ($roamingFolders | Where-Object { $_ } | Select-Object -Unique)) {
            $npmCli = Join-Path $folder ("npm\$Backend.cmd")
            $exists = Test-Path -LiteralPath $npmCli -PathType Leaf
            Write-ReleaseStartupDiagnostic $LogRoot @{ event = 'cli-probe'; backend = $Backend; path = $npmCli; exists = $exists }
            if ($exists) { return $npmCli }
        }
        $cachedCli = Find-ReleaseCachedNpmCli $env:LOCALAPPDATA $Backend -LogRoot $LogRoot
        if ($cachedCli) { return $cachedCli }
    }
    $knownBinary = if ($Backend -eq 'agy' -and $env:LOCALAPPDATA) {
        Join-Path $env:LOCALAPPDATA 'agy\bin\agy.exe'
    } elseif ($Backend -eq 'kimi' -and $env:USERPROFILE) {
        Join-Path $env:USERPROFILE '.kimi-code\bin\kimi.exe'
    } else { $null }
    if ($knownBinary -and (Test-Path -LiteralPath $knownBinary -PathType Leaf)) {
        Write-ReleaseStartupDiagnostic $LogRoot @{ event = 'cli-resolved'; backend = $Backend; cli = $knownBinary; source = 'user-installation' }
        return $knownBinary
    }
    throw "AI CLI $Backend is missing. Install and sign in to that CLI, then reopen the game."
}

function Find-ReleaseNpmNode([string]$Binary, [string]$Backend) {
    # Match the npm shim's preference for node.exe beside the shim, then PATH.
    $adjacent = Join-Path (Split-Path -Parent $Binary) 'node.exe'
    if (Test-Path -LiteralPath $adjacent -PathType Leaf) { return $adjacent }
    $node = Get-Command 'node.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($node) { return $node.Source }
    $programFolders = @($env:ProgramW6432, $env:ProgramFiles, [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles))
    foreach ($folder in ($programFolders | Where-Object { $_ } | Select-Object -Unique)) {
        $candidate = Join-Path $folder 'nodejs\node.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    throw "AI CLI $Backend Node.js is missing. Repair the Node.js installation and reopen the game."
}

function Find-ReleaseDshNode([string]$Binary) {
    return Find-ReleaseNpmNode $Binary 'dsh'
}

function Assert-ReleaseAiCli([string]$Binary, [string]$Backend, [string]$LogRoot = '') {
    $entry = $null
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $Binary
    $start.Arguments = '--version'
    if ($Backend -in @('dsh', 'mcode') -and [IO.Path]::GetExtension($Binary) -eq '.cmd') {
        $nodeBinary = Find-ReleaseNpmNode $Binary $Backend
        $entryPath = if ($Backend -eq 'mcode') { 'node_modules/@minimax-ai/code/cli.js' } else { 'node_modules/@deepseek-ai/dsh/lib/bin.js' }
        $entry = Join-Path (Split-Path -Parent $Binary) $entryPath
        if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
            throw "AI CLI $Backend entry point is missing. Repair the $Backend installation and reopen the game."
        }
        $start.FileName = $nodeBinary
        $start.Arguments = '"' + $entry + '" --version'
        # The runtime's CLI launchers also resolve node through PATH. Bind the
        # same checked installation for this child service, without global edits.
        $env:PATH = (Split-Path -Parent $nodeBinary) + [IO.Path]::PathSeparator + $env:PATH
    }
    Write-ReleaseStartupDiagnostic $LogRoot @{ event = 'cli-check'; backend = $Backend; executable = $start.FileName; entry = $entry }
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    try {
        try { [void]$process.Start() } catch { throw "AI CLI $Backend could not start. Repair that CLI installation and reopen the game." }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(5000)) {
            # This is the --version child created above, never the player's game.
            $process.Kill()
            throw "AI CLI $Backend did not respond to its startup check. Repair that CLI installation and reopen the game."
        }
        if ($process.ExitCode -ne 0) { throw "AI CLI $Backend failed its startup check. Repair that CLI installation and reopen the game." }
    } finally { $process.Dispose() }
}

function Start-ReleaseCompanion([string]$Root, [switch]$CheckOnly, [string[]]$ForwardArgs) {
    $package = Read-VerifiedRelease $Root -FullVerify:$CheckOnly
    $python = Get-ReleasePath $Root $package.python
    if ($ForwardArgs | Where-Object { $_ -match '^--run-dir(=|$)' }) { throw 'Release is bound to its own Mod directory.' }
    $configPath = Join-Path $Root 'config/chat-backend.json'
    if (-not (Test-Path -LiteralPath $configPath)) { throw 'Run the setup entry first to select your AI backend and model.' }
    $settings = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($settings.backend -notin @('kimi', 'agy', 'codex', 'dsh', 'mcode')) { throw 'Choose Kimi, agy, Codex, dsh, or mcode in setup first.' }
    Write-ReleaseStartupDiagnostic $Root @{ event = 'startup'; backend = $settings.backend; powershell = (Get-Process -Id $PID).Path; appData = $env:APPDATA; windowsAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::ApplicationData) }
    $cliBinary = Find-ReleaseAiCli $settings.backend -LogRoot $Root
    Assert-ReleaseAiCli $cliBinary $settings.backend -LogRoot $Root
    Write-ReleaseStartupDiagnostic $Root @{ event = 'cli-check-passed'; backend = $settings.backend; cli = $cliBinary }
    $env:PATH = (Split-Path -Parent $cliBinary) + [IO.Path]::PathSeparator + $env:PATH
    if ($CheckOnly) {
        & $python -B -c "import sqlite3,ssl,mcp,win32job; import stardew_ai_runtime.chat_bridge; print('Portable runtime imports OK')"
        if ($LASTEXITCODE -ne 0) { throw 'Packaged Python could not import runtime dependencies.' }
        Write-Host "Selected $($settings.backend) CLI startup check OK."
        Write-Host "Release $($package.version) verified. No service or game started; account and game connection are checked when used."
        return
    }
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
