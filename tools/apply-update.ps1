[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Request, [switch]$Prepare, [switch]$NoRestart)
$ErrorActionPreference = 'Stop'
[AppContext]::SetSwitch('Switch.System.IO.UseLegacyPathHandling', $false)
[AppContext]::SetSwitch('Switch.System.IO.BlockLongPaths', $false)
. (Join-Path $PSScriptRoot 'release-package.ps1')

function Write-UpdateResult([string]$State, [string]$Message) {
    $result = @{ state = $State; message = $Message; time = (Get-Date -Format o) }
    [IO.File]::WriteAllText((Join-Path $target 'data/update-result.json'), ($result | ConvertTo-Json))
}

function Restore-Update($Journal, [string]$Backup) {
    foreach ($file in $Journal) {
        $destination = Get-ReleasePath $target $file.path
        if ($file.existed) {
            $source = Get-ReleasePath $Backup $file.path
            if ((Get-ReleaseHash $source) -ne $file.sha256) { throw 'Recovery backup is damaged.' }
            if ([IO.File]::Exists($destination) -and (Get-ReleaseHash $destination) -eq $file.sha256) { continue }
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination))
            [IO.File]::Copy($source, $destination, $true)
        } elseif ([IO.File]::Exists($destination)) { [IO.File]::Delete($destination) }
    }
}

$target = $null
$mutex = $null
$locked = $false
$restored = $false
try {
    $requestPath = [IO.Path]::GetFullPath($Request)
    $job = Get-Content -LiteralPath $requestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $target = [IO.Path]::GetFullPath($job.target).TrimEnd('\')
    $game = [IO.Path]::GetFullPath($job.game).TrimEnd('\')
    $mods = $game + '\Mods\'
    if (-not $target.StartsWith($mods, [StringComparison]::OrdinalIgnoreCase)) { throw 'Target must be inside the selected game Mods directory.' }
    [void](Get-ReleasePath $target 'manifest.json')
    $executable = Join-Path $game 'StardewModdingAPI.exe'
    if (-not [IO.File]::Exists($executable)) { throw 'SMAPI executable is missing.' }
    $directory = [IO.Path]::GetDirectoryName($requestPath)
    # All writes stay in the explicitly bound installation or its update workspace.
    $updates = $target + '\data\updates\'
    if (-not $directory.StartsWith($updates, [StringComparison]::OrdinalIgnoreCase)) { throw 'Request is outside the update workspace.' }
    for ($probe = $directory; $probe.Length -ge $target.Length; $probe = [IO.Path]::GetDirectoryName($probe)) {
        if ([IO.File]::GetAttributes($probe) -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked update directories are not allowed.' }
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $key = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($target.ToLowerInvariant()))).Replace('-', '') }
    finally { $sha.Dispose() }
    $mutex = New-Object Threading.Mutex($false, ('Local\StardewAIUpdate-' + $key))
    try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'Another update is running.' }

    $journalPath = Join-Path $target 'data/update-transaction.json'
    $pending = $null
    if (Test-Path -LiteralPath $journalPath) {
        $record = Get-Content -LiteralPath $journalPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($record.state -eq 'installing') {
            if (-not ([IO.Path]::GetFullPath($record.backup)).StartsWith($updates, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid recovery backup path.' }
            $pending = $record
            # A successful manual repair/newer installation supersedes an old
            # interrupted transaction; never roll that valid installation back.
            if ($record.previousVersion) {
                try { $validInstalled = Read-VerifiedRelease $target -FullVerify } catch { $validInstalled = $null }
                if ($validInstalled -and $validInstalled.version -ne $record.previousVersion) { $pending = $null }
            }
        }
    }
    # An interrupted copy may have damaged the installed manifest. Its verified
    # backup identifies the target; restoration still waits for every process.
    $identity = if ($pending) { Get-ReleasePath $pending.backup 'manifest.json' } else { Join-Path $target 'manifest.json' }
    $native = Get-Content -LiteralPath $identity -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($native.UniqueID -ne 'CSXizhang.StardewAICompanion') { throw 'Target is not the companion Mod.' }

    $stage = Join-Path $directory 'stage'
    if ($job.archive) {
        $archive = [IO.Path]::GetFullPath($job.archive)
        if ($archive -ne (Join-Path $directory 'package.zip') -or (Get-ReleaseHash $archive) -ne $job.sha256) { throw 'Update archive checksum mismatch.' }
        if ($Prepare) {
            if (Test-Path -LiteralPath $stage) { throw 'Update stage already exists; download again.' }
            [void][IO.Directory]::CreateDirectory($stage)
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $zip = [IO.Compression.ZipFile]::OpenRead($archive)
            try {
                $seen = @{}
                [long]$total = 0
                foreach ($entry in $zip.Entries) {
                    $name = $entry.FullName.Replace('\', '/')
                    if ($name.EndsWith('/')) { continue }
                    $prefix = 'StardewAI.Companion.Mod/'
                    if (-not $name.StartsWith($prefix, [StringComparison]::Ordinal)) { throw 'Unexpected archive root.' }
                    $relative = $name.Substring($prefix.Length)
                    if ($seen.ContainsKey($relative)) { throw 'Duplicate archive entry.' }
                    $seen[$relative] = $true
                    $total += $entry.Length
                    if ($total -gt 2GB) { throw 'Expanded archive is too large.' }
                    $path = Get-ReleasePath $stage $relative
                    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path))
                    [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $path, $false)
                }
            } finally { $zip.Dispose() }
        }
        $package = Read-VerifiedRelease $stage -FullVerify
        if ($package.version -ne $job.version -or [version]$package.version -le [version]$job.installedVersion) { throw 'Unexpected update version.' }
        $nextNative = Get-Content -LiteralPath (Join-Path $stage 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($nextNative.UniqueID -ne $native.UniqueID -or [version]$nextNative.MinimumApiVersion -gt [version]$job.smapi) { throw 'Update requires a newer SMAPI.' }
        if ($Prepare) { Write-UpdateResult 'prepared' '更新已校验，等待退出游戏。'; exit 0 }
    } elseif ($Prepare) { throw 'No update archive.' }

    # Acknowledge preparation before the Mod exits; failures above leave the game running.
    [IO.File]::WriteAllText($requestPath + '.ready', 'ready')
    # Wait for this exact process, never terminate a game or an unrelated service.
    $running = Get-Process -Id $job.processId -ErrorAction SilentlyContinue
    if ($running -and $running.StartTime.ToUniversalTime().ToString('o') -eq $job.processStarted) {
        if (-not $running.WaitForExit(300000)) { throw 'Game did not exit; installation cancelled.' }
    }
    $deadline = (Get-Date).AddSeconds(90)
    do {
        $busy = @(Get-Process -Name 'StardewModdingAPI', 'Stardew Valley', 'python*' -ErrorAction SilentlyContinue | Where-Object {
            ($_.ProcessName -in @('StardewModdingAPI', 'Stardew Valley') -and $_.Path -and
                $_.Path.StartsWith($game + '\', [StringComparison]::OrdinalIgnoreCase)) -or
            ($_.ProcessName -match '^python' -and $_.Path -and $_.Path.StartsWith($target + '\', [StringComparison]::OrdinalIgnoreCase))
        })
        if ($busy.Count -eq 0) { break }
        if ((Get-Date) -gt $deadline) { throw 'Game or companion service is still running; installation cancelled.' }
        Start-Sleep -Milliseconds 500
    } while ($true)

    if ($job.archive) {
        if ($pending) { Restore-Update $pending.files $pending.backup }
        $installed = Read-VerifiedRelease $target -FullVerify
        if ($installed.version -ne $job.installedVersion) { throw 'Installed version changed; check updates again.' }
        $backup = Join-Path $directory ('backup-' + [Guid]::NewGuid().ToString('N'))
        $paths = @($installed.files.path) + @($package.files.path) + @('release-manifest.json')
        $journal = @()
        foreach ($relative in ($paths | Sort-Object -Unique)) {
            $path = Get-ReleasePath $target $relative
            $exists = [IO.File]::Exists($path)
            $hash = $null
            if ($exists) {
                $copy = Get-ReleasePath $backup $relative
                [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($copy))
                [IO.File]::Copy($path, $copy)
                $hash = Get-ReleaseHash $path
                if ((Get-ReleaseHash $copy) -ne $hash) { throw 'Backup checksum mismatch.' }
            }
            $journal += @{ path = $relative; existed = $exists; sha256 = $hash }
        }
        $transaction = @{ state = 'installing'; backup = $backup; files = $journal; previousVersion = $installed.version; nextVersion = $package.version }
        [IO.File]::WriteAllText($journalPath + '.tmp', ($transaction | ConvertTo-Json -Depth 8))
        Move-Item -LiteralPath ($journalPath + '.tmp') -Destination $journalPath -Force
        try {
            foreach ($entry in $package.files) {
                $destination = Get-ReleasePath $target $entry.path
                [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination))
                [IO.File]::Copy((Get-ReleasePath $stage $entry.path), $destination, $true)
            }
            foreach ($entry in $installed.files) {
                if ($entry.path -notin $package.files.path) { [IO.File]::Delete((Get-ReleasePath $target $entry.path)) }
            }
            [IO.File]::Copy((Join-Path $stage 'release-manifest.json'), (Join-Path $target 'release-manifest.json'), $true)
            [void](Read-VerifiedRelease $target -FullVerify)
            $transaction.state = 'complete'
            [IO.File]::WriteAllText($journalPath, ($transaction | ConvertTo-Json -Depth 8))
            Write-UpdateResult 'complete' '更新完成。'
        } catch {
            $failure = $_
            Restore-Update $journal $backup
            [void](Read-VerifiedRelease $target -FullVerify)
            $transaction.state = 'restored'
            [IO.File]::WriteAllText($journalPath, ($transaction | ConvertTo-Json -Depth 8))
            Write-UpdateResult 'restored' '更新失败，已恢复原版本。'
            $restored = $true
            if (-not $NoRestart) { Start-Process -FilePath $executable -WorkingDirectory $game -WindowStyle Normal }
            throw $failure
        }
    }
    if (-not $NoRestart) { Start-Process -FilePath $executable -WorkingDirectory $game -WindowStyle Normal }
} catch {
    if (-not $restored -and $target -and [IO.Directory]::Exists((Join-Path $target 'data'))) {
        try { Write-UpdateResult 'failed' $_.Exception.Message } catch { }
    }
    Write-Error $_
    exit 1
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}
