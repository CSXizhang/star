[CmdletBinding()]
param(
    [string]$GameDir = '', [string]$TargetModDir = '',
    [ValidateSet('kimi', 'agy', 'none')][string]$Agent = 'kimi',
    [string]$Model = '', [switch]$AutoInstall, [switch]$DryRun, [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$OutputEncoding = New-Object Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$releaseRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $PSScriptRoot 'release-package.ps1')
Write-Host '正在检查伙伴版本与必要文件……'
$package = Read-VerifiedRelease $releaseRoot
Write-Host '版本配对检查完成。'
$agentSpecified = $PSBoundParameters.ContainsKey('Agent')
$modelSpecified = $PSBoundParameters.ContainsKey('Model')

function Read-BackendSettings([string]$SelectedGame) {
    if (-not $SelectedGame) { return $null }
    $file = Join-Path $SelectedGame 'Mods\StardewAI.Companion.Mod\config\chat-backend.json'
    if (Test-Path -LiteralPath $file) {
        return Get-Content -LiteralPath $file -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    return $null
}

if ($CheckOnly) {
    Start-ReleaseCompanion $releaseRoot -CheckOnly
    exit 0
}
if (-not $GameDir) {
    Write-Host '正在查找星露谷游戏目录……'
    $parentGame = Split-Path (Split-Path $releaseRoot -Parent) -Parent
    if (Test-Path -LiteralPath (Join-Path $parentGame 'StardewModdingAPI.exe')) { $GameDir = $parentGame }
    else {
        $detected = & (Join-Path $PSScriptRoot 'detect-game.ps1')
        if ($detected) { $GameDir = ($detected | Out-String).Trim() }
    }
}

if (-not $AutoInstall -and -not $DryRun) {
    Write-Host '正在打开设置窗口；如果窗口未显示，请在任务栏查找“星露谷伙伴下载版设置”。'
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [Windows.Forms.Application]::EnableVisualStyles()
    $form = New-Object Windows.Forms.Form
    $form.Text = '星露谷伙伴下载版设置'
    $form.Size = New-Object Drawing.Size(640, 340)
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $label = New-Object Windows.Forms.Label
    $label.Text = '游戏目录（包含 StardewModdingAPI.exe）'
    $label.SetBounds(20, 20, 580, 25)
    $form.Controls.Add($label)
    $pathBox = New-Object Windows.Forms.TextBox
    $pathBox.Text = $GameDir
    $pathBox.SetBounds(20, 50, 480, 26)
    $form.Controls.Add($pathBox)
    $browse = New-Object Windows.Forms.Button
    $browse.Text = '浏览'
    $browse.SetBounds(515, 48, 85, 28)
    $browse.Add_Click({
        $dialog = New-Object Windows.Forms.FolderBrowserDialog
        if ($dialog.ShowDialog() -eq 'OK') { $pathBox.Text = $dialog.SelectedPath }
        $dialog.Dispose()
    })
    $form.Controls.Add($browse)
    $backendBox = New-Object Windows.Forms.ComboBox
    $backendBox.DropDownStyle = 'DropDownList'
    [void]$backendBox.Items.AddRange(@('kimi', 'agy'))
    $backendBox.SelectedItem = if ($Agent -eq 'agy') { 'agy' } else { 'kimi' }
    $backendBox.SetBounds(20, 105, 150, 28)
    $form.Controls.Add($backendBox)
    $modelBox = New-Object Windows.Forms.TextBox
    $modelBox.Text = if ($Model) { $Model } elseif ($Agent -eq 'agy') { 'gemini-3.8-flash' } else { 'kimi-code/k3' }
    $modelBox.SetBounds(190, 105, 410, 28)
    $form.Controls.Add($modelBox)
    $backendBox.Add_SelectedIndexChanged({
        $modelBox.Text = if ($backendBox.SelectedItem -eq 'agy') { 'gemini-3.8-flash' } else { 'kimi-code/k3' }
    })
    $loadSettings = {
        $saved = Read-BackendSettings $pathBox.Text.Trim()
        if ($saved -and $saved.backend -in @('kimi', 'agy')) {
            if (-not $agentSpecified) { $backendBox.SelectedItem = $saved.backend }
            if (-not $modelSpecified -and $saved.model -and (-not $agentSpecified -or $Agent -eq $saved.backend)) { $modelBox.Text = $saved.model }
        }
    }
    $pathBox.Add_TextChanged($loadSettings)
    & $loadSettings
    $note = New-Object Windows.Forms.Label
    $note.Text = "左侧选择 AI 客户端，右侧填写该账号可用的模型标识。`nKimi 写入 Mod 内项目 MCP 配置；agy 注册客户端级 stardew-companion。`n请自行安装并登录所选 CLI。此向导不会安装 CLI，也不会复制账号凭据。`n更新保留玩家 data、已有设置与存档；请先退出游戏和伙伴服务。"
    $note.SetBounds(20, 145, 580, 95)
    $form.Controls.Add($note)
    $ok = New-Object Windows.Forms.Button
    $ok.Text = '安装并应用设置'
    $ok.SetBounds(380, 252, 220, 34)
    $ok.DialogResult = [Windows.Forms.DialogResult]::OK
    $form.Controls.Add($ok)
    $form.AcceptButton = $ok
    $form.Add_Shown({
        $form.TopMost = $true
        $form.Activate()
        $form.BringToFront()
        $form.TopMost = $false
        Write-Host '设置窗口已打开。关闭窗口即可取消，不会安装或修改配置。'
    })
    if ($form.ShowDialog() -ne 'OK') { $form.Dispose(); exit 0 }
    $GameDir = $pathBox.Text.Trim()
    $Agent = [string]$backendBox.SelectedItem
    $Model = $modelBox.Text.Trim()
    $agentSpecified = $true
    $modelSpecified = $true
    $form.Dispose()
}

if (-not $GameDir -or -not (Test-Path -LiteralPath (Join-Path $GameDir 'StardewModdingAPI.exe') -PathType Leaf)) {
    throw 'Select a game directory containing StardewModdingAPI.exe. Install SMAPI first.'
}
$gamePath = [IO.Path]::GetFullPath($GameDir)
$destination = Join-Path $gamePath 'Mods\StardewAI.Companion.Mod'
$previousSettings = Read-BackendSettings $gamePath
if ($previousSettings) {
    if (-not $agentSpecified -and $previousSettings.backend -in @('kimi', 'agy')) { $Agent = $previousSettings.backend }
    if (-not $modelSpecified -and $previousSettings.model -and (-not $agentSpecified -or $Agent -eq $previousSettings.backend)) { $Model = $previousSettings.model }
}
if ($TargetModDir -and [IO.Path]::GetFullPath($TargetModDir).TrimEnd('\') -ne $destination.TrimEnd('\')) { throw 'Release installs only into the selected game Mods/StardewAI.Companion.Mod directory.' }
if ($DryRun) {
    [PSCustomObject]@{ version = $package.version; destination = $destination; files = @($package.files).Count; backend = $Agent; dryRun = $true } | ConvertTo-Json
    exit 0
}
if (Get-Process -Name 'StardewModdingAPI', 'Stardew Valley' -ErrorAction SilentlyContinue) { throw 'Close Stardew Valley and SMAPI before installing. No process will be stopped.' }
if (-not $Model) { $Model = if ($Agent -eq 'agy') { 'gemini-3.8-flash' } else { 'kimi-code/k3' } }

if ($destination.TrimEnd('\') -ne $releaseRoot.TrimEnd('\')) {
    # Preflight every destination before any copying; never follow directory links.
    foreach ($entry in $package.files) { [void](Get-ReleasePath $destination $entry.path) }
    [void](Get-ReleasePath $destination 'release-manifest.json')
    $previousManifest = Join-Path $destination 'release-manifest.json'
    $oldEntries = @()
    if (Test-Path -LiteralPath $previousManifest) {
        $old = Get-Content -LiteralPath $previousManifest -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($old.manifestType -ne 'windows-release') { throw 'Installed release manifest is invalid.' }
        $oldEntries = @($old.files)
        foreach ($entry in $oldEntries) { [void](Get-ReleasePath $destination $entry.path) }
    }
    $backup = Join-Path $destination ('data\release-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
    $knownFiles = @($package.files.path) + @($oldEntries.path) + @('release-manifest.json')
    foreach ($relative in ($knownFiles | Sort-Object -Unique)) {
        $existing = Get-ReleasePath $destination $relative
        if (Test-Path -LiteralPath $existing -PathType Leaf) {
            $backupFile = Join-Path $backup $relative
            [void][IO.Directory]::CreateDirectory((Split-Path $backupFile -Parent))
            Copy-Item -LiteralPath $existing -Destination $backupFile
        }
    }
    foreach ($entry in $package.files) {
        $target = Get-ReleasePath $destination $entry.path
        [void][IO.Directory]::CreateDirectory((Split-Path $target -Parent))
        Copy-Item -LiteralPath (Get-ReleasePath $releaseRoot $entry.path) -Destination $target -Force
    }
    # Delete only explicitly tracked obsolete files, after backing them up.
    foreach ($entry in $oldEntries) {
        if ($entry.path -notin $package.files.path) {
            $stale = Get-ReleasePath $destination $entry.path
            if (Test-Path -LiteralPath $stale -PathType Leaf) { Remove-Item -LiteralPath $stale }
        }
    }
    Copy-Item -LiteralPath (Join-Path $releaseRoot 'release-manifest.json') -Destination $previousManifest -Force
}
[void](Read-VerifiedRelease $destination)
if ($Agent -ne 'none') {
    $configDir = Join-Path $destination 'config'
    [void][IO.Directory]::CreateDirectory($configDir)
    $configFile = Join-Path $configDir 'chat-backend.json'
    if (Test-Path -LiteralPath $configFile) { Copy-Item -LiteralPath $configFile -Destination ($configFile + '.previous') -Force }
    $merged = @{}
    if ($previousSettings) {
        foreach ($property in $previousSettings.PSObject.Properties) { $merged[$property.Name] = $property.Value }
    }
    $merged.backend = $Agent
    $merged.model = $Model
    [IO.File]::WriteAllText($configFile, ($merged | ConvertTo-Json -Depth 20), (New-Object Text.UTF8Encoding($false)))
    if ($Agent -eq 'kimi' -or (Get-Command agy.exe -ErrorAction SilentlyContinue)) {
        & (Join-Path $destination 'tools/register-mcp.ps1') -RunDir $destination -Agent $Agent -Install
    } else {
        Write-Warning 'agy CLI is missing. Install and sign in to agy, then rerun setup to register MCP.'
    }
    if (-not (Get-Command ($Agent + '.exe') -ErrorAction SilentlyContinue)) {
        Write-Warning "Install and sign in to $Agent CLI. Setup does not install third-party clients or accounts."
    }
}
Write-Host "Release $($package.version) installed at $destination"
Write-Host 'Kimi users: open the installed Mod folder in interactive Kimi and trust that project/MCP before playing.'
Write-Host 'After CLI login/setup, start SMAPI and load a save. The Mod starts its paired service automatically.'
