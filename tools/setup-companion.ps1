<#
.SYNOPSIS
    星露谷 AI 伙伴一键设置向导 (Stardew AI Companion Setup Wizard)
.DESCRIPTION
    图形化/自动化配置工具：自动检测游戏目录、安全安装纯净生产 Mod、向 AI 客户端注册 MCP 服务。
    支持 Windows 原生 WinForms 界面双击运行，亦支持命令行参数无人值守自动化测试。
#>
[CmdletBinding()]
param(
    [string]$GameDir = "",
    [string]$TargetModDir = "",
    [ValidateSet('agy', 'kimi', 'claude', 'all', 'none')][string]$Agent = 'kimi',
    [switch]$AutoInstall,
    [switch]$DryRun,
    [switch]$CheckOnly,
    [string]$CandidateManifest = ""
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = (New-Object System.Text.UTF8Encoding($false))
[Console]::OutputEncoding = (New-Object System.Text.UTF8Encoding($false))

# 1. 路径与常量解析 (基于脚本位置动态推导，杜绝个人绝对路径常量)
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
$distSourceDir = Join-Path $repoRoot "artifacts\dist\StardewAI.Companion.Mod"
$runtimeDir = Join-Path $repoRoot "runtime"
$detectScript = Join-Path $scriptDir "detect-game.ps1"
. (Join-Path $scriptDir 'candidate-package.ps1')
$candidatePackage = $null
if ($CandidateManifest) {
    $candidatePackage = Read-VerifiedCandidate $repoRoot $CandidateManifest -AllowUnaccepted:($DryRun -or $CheckOnly)
    $distSourceDir = $candidatePackage.ResolvedModSource
}


# 2. 核心辅助函数
function Find-StardewDirectory {
    if ($GameDir) {
        if (Test-Path -LiteralPath $GameDir) {
            return (Resolve-Path -LiteralPath $GameDir).Path
        } else {
            throw "指定的 GameDir 目录不存在: $GameDir"
        }
    }
    if (Test-Path -LiteralPath $detectScript) {
        $detected = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $detectScript
        if ($detected -and (Test-Path -LiteralPath $detected.Trim())) {
            return (Resolve-Path -LiteralPath $detected.Trim()).Path
        }
    }
    # 默认兜底检测
    $fallbacks = @(
        "E:\Game\steam\steamapps\common\Stardew Valley",
        "C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley",
        "C:\Program Files\Steam\steamapps\common\Stardew Valley",
        "D:\SteamLibrary\steamapps\common\Stardew Valley"
    )
    foreach ($fb in $fallbacks) {
        if (Test-Path -LiteralPath $fb) {
            return (Resolve-Path -LiteralPath $fb).Path
        }
    }
    return $null
}

function Get-RunningGameProcess {
    $procs = Get-Process -Name "StardewModdingAPI", "Stardew Valley" -ErrorAction SilentlyContinue
    if ($procs) {
        return $procs[0]
    }
    return $null
}

function Get-Sha256([string]$filePath) {
    if (Get-Command Get-FileHash -ErrorAction SilentlyContinue) {
        return (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash
    }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($filePath)
    try {
        $bytes = $sha.ComputeHash($stream)
        return (-join ($bytes | ForEach-Object { '{0:X2}' -f $_ }))
    } finally {
        $stream.Close()
        $sha.Dispose()
    }
}

function Get-ModStatusInfo([string]$gamePath, [string]$overrideModDir = "") {
    $targetDir = if ($overrideModDir) { $overrideModDir } else { Join-Path $gamePath "Mods\StardewAI.Companion.Mod" }
    $smapiPath = Join-Path $gamePath "StardewModdingAPI.exe"
    $hasSmapi = Test-Path -LiteralPath $smapiPath
    $modInstalled = Test-Path -LiteralPath (Join-Path $targetDir "manifest.json")
    $hasDiscovery = (Test-Path -LiteralPath (Join-Path $targetDir "data\transport-discovery.json"))
    
    $isUpToDate = $false
    if ($modInstalled -and (Test-Path -LiteralPath $distSourceDir)) {
        $srcDll = Join-Path $distSourceDir "StardewAI.Companion.Mod.dll"
        $dstDll = Join-Path $targetDir "StardewAI.Companion.Mod.dll"
        if ((Test-Path -LiteralPath $srcDll) -and (Test-Path -LiteralPath $dstDll)) {
            $srcHash = Get-Sha256 $srcDll
            $dstHash = Get-Sha256 $dstDll
            $isUpToDate = ($srcHash -eq $dstHash)
        }
    }

    return [PSCustomObject]@{
        GamePath      = $gamePath
        SmapiPath     = $smapiPath
        HasSmapi      = $hasSmapi
        TargetModDir  = $targetDir
        ModInstalled  = $modInstalled
        IsUpToDate    = $isUpToDate
        HasDiscovery  = $hasDiscovery
    }
}

function Install-ModFiles([string]$sourceDir, [string]$destinationDir, [bool]$isDryRun = $false) {
    if (-not (Test-Path -LiteralPath $sourceDir)) {
        throw "生产发布包源目录不存在: $sourceDir"
    }

    $prodFiles = @(
        "manifest.json",
        "StardewAI.Companion.Mod.dll",
        "StardewAI.Companion.Mod.pdb",
        "StardewAI.Companion.Mod.deps.json"
    )

    if ($candidatePackage) { Assert-FormalCandidateTarget $detectedGame $destinationDir }
    if ($isDryRun) {
        return [PSCustomObject]@{
            Success = $true
            CopiedFiles = $prodFiles
            Message = "DryRun: 模拟复制 4 个纯净生产文件至 $destinationDir"
        }
    }

    if (Get-RunningGameProcess) { throw "Close Stardew Valley and SMAPI before installing. No process will be stopped." }
    if ($candidatePackage) { Assert-FormalCandidateTarget $detectedGame $destinationDir }
    foreach ($fileName in $prodFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $sourceDir $fileName) -PathType Leaf)) { throw "Missing candidate file: $fileName" }
    }
    # 安全创建目标目录，绝不删除已有 data/ 目录或存档
    if (-not (Test-Path -LiteralPath $destinationDir)) {
        New-Item -ItemType Directory -Path $destinationDir -Force | Out-Null
    }

    $candidateBackup = $null
    if ($candidatePackage -and (Test-Path -LiteralPath $destinationDir)) {
        $candidateBackup = Join-Path $repoRoot ("artifacts\user-install-backups\" + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
        [void][IO.Directory]::CreateDirectory($candidateBackup)
        foreach ($fileName in $prodFiles) {
            $oldFile = Join-Path $destinationDir $fileName
            if (Test-Path -LiteralPath $oldFile -PathType Leaf) { Copy-Item -LiteralPath $oldFile -Destination (Join-Path $candidateBackup $fileName) }
        }
    }
    $copied = @()
    foreach ($f in $prodFiles) {
        $srcFile = Join-Path $sourceDir $f
        if (Test-Path -LiteralPath $srcFile) {
            $dstFile = Join-Path $destinationDir $f
            Copy-Item -LiteralPath $srcFile -Destination $dstFile -Force
            $copied += $f
        } else {
            throw "关键生产文件缺失: $srcFile"
        }
    }

    if ($candidatePackage) {
        $installed = @{
            version = $candidatePackage.version
            backupDirectory = $candidateBackup
            manifest = [IO.Path]::GetFullPath($CandidateManifest)
            gameDirectory = $detectedGame
            modDirectory = [IO.Path]::GetFullPath($destinationDir)
        }
        $configDirectory = Join-Path $repoRoot 'config'
        [void][IO.Directory]::CreateDirectory($configDirectory)
        [IO.File]::WriteAllText((Join-Path $configDirectory 'installed-candidate.json'), ($installed | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
    } else {
        # 自建绑定路径：安装自编译产物后计算 SHA256，生成 local-dev 清单并写入 config/installed-candidate.json。
        # 仅当目标是检测到的真实游戏 Mod 目录时才写仓库级绑定；显式 -TargetModDir 的自定义/测试安装不触碰绑定。
        $normDestForBinding = [IO.Path]::GetFullPath($destinationDir).TrimEnd('\')
        $writeBinding = $false
        if ($detectedGame) {
            $normExpectedForBinding = [IO.Path]::GetFullPath((Join-Path $detectedGame 'Mods\StardewAI.Companion.Mod')).TrimEnd('\')
            $writeBinding = ($normDestForBinding -eq $normExpectedForBinding)
        }
        if (-not $writeBinding) {
            Write-Host "目标不是检测到的游戏 Mod 目录，跳过本地绑定写入（config/installed-candidate.json 保持不变）"
        } else {
        $installedDll = Join-Path $destinationDir "StardewAI.Companion.Mod.dll"
        $modSha256 = Get-Sha256 $installedDll
        $localDevDir = Join-Path $repoRoot "artifacts\releases\local-dev"
        [void][IO.Directory]::CreateDirectory($localDevDir)
        $localDevManifest = Join-Path $localDevDir "manifest.json"

        $filesList = @()
        foreach ($fn in $prodFiles) {
            $distFilePath = "artifacts/dist/StardewAI.Companion.Mod/$fn"
            $fullDistFilePath = Join-Path $repoRoot ("artifacts\dist\StardewAI.Companion.Mod\" + $fn)
            if (Test-Path -LiteralPath $fullDistFilePath) {
                $filesList += [ordered]@{
                    path = $distFilePath
                    sha256 = Get-Sha256 $fullDistFilePath
                }
            }
        }

        $manifestData = [ordered]@{
            version = "local-dev"
            manifestType = "local-dev"
            acceptancePassed = $true
            acceptanceScope = "local-dev source build; developer verified"
            modSource = "artifacts/dist/StardewAI.Companion.Mod"
            modSha256 = $modSha256
            backend = "preserve-existing"
            files = $filesList
        }
        $manifestJson = $manifestData | ConvertTo-Json -Depth 5
        [IO.File]::WriteAllText($localDevManifest, $manifestJson, (New-Object Text.UTF8Encoding($false)))

        $effectiveGame = $detectedGame
        if ($destinationDir -match '[\\/]Mods[\\/][^\\/]+$') {
            $inferredGame = Split-Path (Split-Path $destinationDir -Parent) -Parent
            if ($detectedGame) {
                $normDest = [IO.Path]::GetFullPath($destinationDir).TrimEnd('\')
                $normDetectedExpected = [IO.Path]::GetFullPath((Join-Path $detectedGame 'Mods\StardewAI.Companion.Mod')).TrimEnd('\')
                if ($normDest -ne $normDetectedExpected) {
                    $effectiveGame = $inferredGame
                }
            } else {
                $effectiveGame = $inferredGame
            }
        }

        $installed = [ordered]@{
            version = "local-dev"
            manifestType = "local-dev"
            backupDirectory = $null
            manifest = [IO.Path]::GetFullPath($localDevManifest)
            gameDirectory = $effectiveGame
            modDirectory = [IO.Path]::GetFullPath($destinationDir)
        }
        $configDirectory = Join-Path $repoRoot 'config'
        [void][IO.Directory]::CreateDirectory($configDirectory)
        [IO.File]::WriteAllText((Join-Path $configDirectory 'installed-candidate.json'), ($installed | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
        }
    }

    return [PSCustomObject]@{
        Success = $true
        CopiedFiles = $copied
        Message = "成功安装/更新 $($copied.Count) 个纯净生产文件 (data 目录完好保留)"
    }
}

function Register-Mcp([string]$agentName, [string]$modDir, [bool]$isDryRun = $false) {
    $serverName = "stardew-companion"
    $runtimePython = Join-Path $runtimeDir ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $runtimePython)) {
        throw "运行时 Python 不存在: $runtimePython"
    }
    $mcpArgs = @("-m", "stardew_ai_runtime.mcp_server", "--run-dir", $modDir)

    if ($isDryRun) {
        return [PSCustomObject]@{
            Agent = $agentName
            Success = $true
            Command = "$runtimePython $($mcpArgs -join ' ')"
            Message = "DryRun: 模拟注册 $serverName 到 $agentName"
        }
    }

    switch ($agentName) {
        'agy' {
            # 优先使用 agy CLI 注册
            $existing = & agy mcp list 2>&1 | Out-String
            if ($existing -match $serverName) {
                & agy mcp remove $serverName 2>&1 | Out-Null
            }
            & agy mcp add $serverName -- $runtimePython @mcpArgs
            $check = & agy mcp list 2>&1 | Out-String
            $ok = ($check -match $serverName)
            return [PSCustomObject]@{
                Agent = 'agy'
                Success = $ok
                Message = if ($ok) { "成功将 $serverName 注册至 agy CLI" } else { "注册后在 agy 列表中未发现 $serverName" }
            }
        }
        'kimi' {
            $kimiDir = Join-Path $repoRoot ".kimi-code"
            $mcpFile = Join-Path $kimiDir "mcp.json"
            New-Item -ItemType Directory -Path $kimiDir -Force | Out-Null
            $serverMap = @{}
            if (Test-Path -LiteralPath $mcpFile) {
                try {
                    $raw = Get-Content -LiteralPath $mcpFile -Raw -Encoding utf8
                    $parsed = ConvertFrom-Json -InputObject $raw
                    if ($parsed.mcpServers) {
                        foreach ($prop in $parsed.mcpServers.PSObject.Properties) {
                            $serverMap[$prop.Name] = @{
                                command = [string]$prop.Value.command
                                args = @($prop.Value.args | ForEach-Object { [string]$_ })
                            }
                        }
                    }
                } catch {}
            }
            $serverMap[$serverName] = @{ command = $runtimePython; args = $mcpArgs }
            $config = @{ mcpServers = $serverMap }
            $jsonText = $config | ConvertTo-Json -Depth 10
            [System.IO.File]::WriteAllText($mcpFile, $jsonText, (New-Object System.Text.UTF8Encoding($false)))
            return [PSCustomObject]@{
                Agent = 'kimi'
                Success = $true
                Message = "已成功写入项目级配置: $mcpFile"
            }
        }
        'claude' {
            $claudeDir = Join-Path $env:APPDATA "Claude"
            $cfgFile = Join-Path $claudeDir "claude_desktop_config.json"
            $serverMap = @{}
            if (Test-Path -LiteralPath $cfgFile) {
                try {
                    $raw = Get-Content -LiteralPath $cfgFile -Raw -Encoding utf8
                    $parsed = ConvertFrom-Json -InputObject $raw
                    if ($parsed.mcpServers) {
                        foreach ($prop in $parsed.mcpServers.PSObject.Properties) {
                            $serverMap[$prop.Name] = @{
                                command = [string]$prop.Value.command
                                args = @($prop.Value.args | ForEach-Object { [string]$_ })
                            }
                        }
                    }
                } catch {}
            }
            $serverMap[$serverName] = @{ command = $runtimePython; args = $mcpArgs }
            $config = @{ mcpServers = $serverMap }
            if (-not (Test-Path -LiteralPath $claudeDir)) {
                New-Item -ItemType Directory -Path $claudeDir -Force | Out-Null
            }
            $jsonText = $config | ConvertTo-Json -Depth 10
            [System.IO.File]::WriteAllText($cfgFile, $jsonText, (New-Object System.Text.UTF8Encoding($false)))
            return [PSCustomObject]@{
                Agent = 'claude'
                Success = $true
                Message = "已写入 Claude Desktop 配置: $cfgFile"
            }
        }
        default {
            return [PSCustomObject]@{
                Agent = $agentName
                Success = $true
                Message = "跳过自动注册"
            }
        }
    }
}

function Set-ChatBackendConfig([string]$backendName, [bool]$isDryRun = $false) {
    if ($backendName -notin @('agy', 'kimi')) {
        return
    }
    $configDir = Join-Path $repoRoot "config"
    $configFile = Join-Path $configDir "chat-backend.json"
    if ($isDryRun) {
        Write-Host "DryRun: 模拟写入聊天后端配置 $backendName"
        return
    }
    if (-not (Test-Path -LiteralPath $configDir)) {
        New-Item -ItemType Directory -Path $configDir -Force | Out-Null
    }
    $model = if ($backendName -eq 'kimi') { 'kimi-code/k3' } else { 'gemini-3.8-flash' }
    $jsonText = @{ backend = $backendName; model = $model } | ConvertTo-Json
    [System.IO.File]::WriteAllText($configFile, $jsonText, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "聊天后端已配置为 $backendName / $model"
}

# 3. 命令行 / 非图形化模式处理
$detectedGame = Find-StardewDirectory
$runningProc = Get-RunningGameProcess
$targetMod = if ($TargetModDir) { $TargetModDir } elseif ($detectedGame) { Join-Path $detectedGame "Mods\StardewAI.Companion.Mod" } else { "" }

if ($CheckOnly) {
    $info = if ($detectedGame) { Get-ModStatusInfo $detectedGame $targetMod } else { $null }
    $res = [PSCustomObject]@{
        GameFound    = ($detectedGame -ne $null)
        GamePath     = $detectedGame
        IsRunning    = ($runningProc -ne $null)
        RunningPid   = if ($runningProc) { $runningProc.Id } else { $null }
        ModStatus    = $info
    }
    $res | ConvertTo-Json -Depth 5
    exit 0
}

if ($AutoInstall -or $DryRun) {
    $runModeName = if ($DryRun) { 'DryRun' } else { 'AutoInstall' }
    Write-Host "=== 星露谷 AI 伙伴自动化设置 ($runModeName) ==="
    if (-not $detectedGame) {
        throw "未能自动找到星露谷游戏目录。请使用 -GameDir 指定目录。"
    }
    Write-Host "游戏目录: $detectedGame"
    if ($runningProc) {
        Write-Warning "检测到游戏正在运行 (PID: $($runningProc.Id))。若更新被锁定，请先退出游戏。"
    }
    Write-Host "安装 Mod 到: $targetMod"
    $modRes = Install-ModFiles -sourceDir $distSourceDir -destinationDir $targetMod -isDryRun $DryRun
    Write-Host "Mod 状态: $($modRes.Message)"

    if ($Agent -ne 'none') {
        Write-Host "注册 MCP 服务至 $Agent..."
        $mcpRes = Register-Mcp -agentName $Agent -modDir $targetMod -isDryRun $DryRun
        Write-Host "MCP 状态: $($mcpRes.Message)"
        Set-ChatBackendConfig -backendName $Agent -isDryRun $DryRun
    }
    Write-Host "=== 自动化设置完成 ==="
    exit 0
}

# 4. 图形化 WinForms 向导界面
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = "星露谷 AI 伙伴一键设置向导"
$form.Size = New-Object System.Drawing.Size(660, 560)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)

# 顶部标题栏
$pnlHeader = New-Object System.Windows.Forms.Panel
$pnlHeader.Location = New-Object System.Drawing.Point(0, 0)
$pnlHeader.Size = New-Object System.Drawing.Size(660, 70)
$pnlHeader.BackColor = [System.Drawing.Color]::FromArgb(240, 246, 255)
$form.Controls.Add($pnlHeader)

$lblTitle = New-Object System.Windows.Forms.Label
$lblTitle.Text = "星露谷 AI 伙伴 (Stardew AI Companion) 快速设置"
$lblTitle.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 12, [System.Drawing.FontStyle]::Bold)
$lblTitle.Location = New-Object System.Drawing.Point(20, 12)
$lblTitle.AutoSize = $true
$pnlHeader.Controls.Add($lblTitle)

$lblSubtitle = New-Object System.Windows.Forms.Label
$lblSubtitle.Text = "一键完成游戏 Mod 安装与 AI 客户端 MCP 服务注册，无需手动敲命令与配置参数。"
$lblSubtitle.Location = New-Object System.Drawing.Point(22, 40)
$lblSubtitle.AutoSize = $true
$lblSubtitle.ForeColor = [System.Drawing.Color]::FromArgb(80, 80, 80)
$pnlHeader.Controls.Add($lblSubtitle)

# 分组 1: 游戏与 Mod 目录
$grpGame = New-Object System.Windows.Forms.GroupBox
$grpGame.Text = "1. 游戏与模组环境"
$grpGame.Location = New-Object System.Drawing.Point(20, 85)
$grpGame.Size = New-Object System.Drawing.Size(605, 120)
$form.Controls.Add($grpGame)

$lblGamePath = New-Object System.Windows.Forms.Label
$lblGamePath.Text = "游戏安装目录 (含 StardewModdingAPI.exe)："
$lblGamePath.Location = New-Object System.Drawing.Point(15, 25)
$lblGamePath.AutoSize = $true
$grpGame.Controls.Add($lblGamePath)

$txtGamePath = New-Object System.Windows.Forms.TextBox
$txtGamePath.Location = New-Object System.Drawing.Point(15, 48)
$txtGamePath.Size = New-Object System.Drawing.Size(465, 25)
$txtGamePath.Text = if ($detectedGame) { $detectedGame } else { "" }
$grpGame.Controls.Add($txtGamePath)

$btnBrowse = New-Object System.Windows.Forms.Button
$btnBrowse.Text = "浏览/更换..."
$btnBrowse.Location = New-Object System.Drawing.Point(490, 47)
$btnBrowse.Size = New-Object System.Drawing.Size(95, 27)
$grpGame.Controls.Add($btnBrowse)

$lblGameStatus = New-Object System.Windows.Forms.Label
$lblGameStatus.Location = New-Object System.Drawing.Point(15, 83)
$lblGameStatus.Size = New-Object System.Drawing.Size(570, 25)
$grpGame.Controls.Add($lblGameStatus)

# 分组 2: AI 客户端选择
$grpAgent = New-Object System.Windows.Forms.GroupBox
$grpAgent.Text = "2. 选择要接入的 AI 客户端"
$grpAgent.Location = New-Object System.Drawing.Point(20, 215)
$grpAgent.Size = New-Object System.Drawing.Size(605, 110)
$form.Controls.Add($grpAgent)

$rbAgy = New-Object System.Windows.Forms.RadioButton
$rbAgy.Text = "agy CLI（推荐，当前环境自动注册）"
$rbAgy.Location = New-Object System.Drawing.Point(20, 25)
$rbAgy.AutoSize = $true
$rbAgy.Checked = $false
$grpAgent.Controls.Add($rbAgy)

$rbKimi = New-Object System.Windows.Forms.RadioButton
$rbKimi.Text = "Kimi Code（项目级 .kimi-code/mcp.json）"
$rbKimi.Location = New-Object System.Drawing.Point(20, 50)
$rbKimi.AutoSize = $true
$rbKimi.Checked = $true
$grpAgent.Controls.Add($rbKimi)

$rbClaude = New-Object System.Windows.Forms.RadioButton
$rbClaude.Text = "Claude Desktop / Cursor / 其他客户端"
$rbClaude.Location = New-Object System.Drawing.Point(20, 75)
$rbClaude.AutoSize = $true
$grpAgent.Controls.Add($rbClaude)

# 分组 3: 操作日志与提示
$grpLog = New-Object System.Windows.Forms.GroupBox
$grpLog.Text = "3. 状态与操作提示"
$grpLog.Location = New-Object System.Drawing.Point(20, 335)
$grpLog.Size = New-Object System.Drawing.Size(605, 125)
$form.Controls.Add($grpLog)

$txtLog = New-Object System.Windows.Forms.TextBox
$txtLog.Multiline = $true
$txtLog.ReadOnly = $true
$txtLog.ScrollBars = "Vertical"
$txtLog.Location = New-Object System.Drawing.Point(15, 22)
$txtLog.Size = New-Object System.Drawing.Size(575, 90)
$txtLog.BackColor = [System.Drawing.Color]::White
$txtLog.Text = "向导已就绪。点击下方【一键完成设置】即可自动安装 Mod 并注册 MCP 服务。"
$grpLog.Controls.Add($txtLog)

# 底部按钮区
$btnViewUsage = New-Object System.Windows.Forms.Button
$btnViewUsage.Text = "查看 Token 使用记录"
$btnViewUsage.Location = New-Object System.Drawing.Point(20, 475)
$btnViewUsage.Size = New-Object System.Drawing.Size(160, 32)
$btnViewUsage.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
$form.Controls.Add($btnViewUsage)

$btnApply = New-Object System.Windows.Forms.Button
$btnApply.Text = "一键完成设置 (安装 Mod + 注册 MCP)"
$btnApply.Location = New-Object System.Drawing.Point(240, 475)
$btnApply.Size = New-Object System.Drawing.Size(260, 32)
$btnApply.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9, [System.Drawing.FontStyle]::Bold)
$btnApply.BackColor = [System.Drawing.Color]::FromArgb(46, 117, 182)
$btnApply.ForeColor = [System.Drawing.Color]::White
$form.Controls.Add($btnApply)

$btnClose = New-Object System.Windows.Forms.Button
$btnClose.Text = "关闭"
$btnClose.Location = New-Object System.Drawing.Point(515, 475)
$btnClose.Size = New-Object System.Drawing.Size(110, 32)
$form.Controls.Add($btnClose)

# 状态刷新逻辑
function Update-UiStatus {
    $currentPath = $txtGamePath.Text.Trim()
    if (-not $currentPath -or -not (Test-Path -LiteralPath $currentPath)) {
        $lblGameStatus.Text = "❌ 未检测到游戏目录，请点击【浏览/更换...】选择星露谷游戏安装根目录。"
        $lblGameStatus.ForeColor = [System.Drawing.Color]::Red
        return
    }

    $modInfo = Get-ModStatusInfo $currentPath
    $proc = Get-RunningGameProcess
    $statusMsg = ""

    if (-not $modInfo.HasSmapi) {
        $statusMsg = "⚠️ 找到游戏目录但未发现 StardewModdingAPI.exe，请确认是否已安装 SMAPI。"
        $lblGameStatus.ForeColor = [System.Drawing.Color]::DarkOrange
    } else {
        $statusMsg = "✅ SMAPI 就绪；Mod 状态: " + $(if ($modInfo.IsUpToDate) { "已是最新生产版本" } elseif ($modInfo.ModInstalled) { "已安装 (可更新)" } else { "未安装 (点击一键设置自动安装)" })
        $lblGameStatus.ForeColor = [System.Drawing.Color]::FromArgb(0, 128, 0)
    }

    if ($proc) {
        $statusMsg += " | 🎮 游戏正在运行中 (PID: $($proc.Id))"
    }

    $lblGameStatus.Text = $statusMsg
}

# 事件绑定
$btnBrowse.Add_Click({
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = "请选择包含 StardewModdingAPI.exe 的星露谷物语安装根目录："
    $dlg.ShowNewFolderButton = $false
    if ($txtGamePath.Text -and (Test-Path -LiteralPath $txtGamePath.Text)) {
        $dlg.SelectedPath = $txtGamePath.Text
    }
    if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        $txtGamePath.Text = $dlg.SelectedPath
        Update-UiStatus
    }
})

$txtGamePath.Add_TextChanged({ Update-UiStatus })

$btnApply.Add_Click({
    $currentPath = $txtGamePath.Text.Trim()
    if (-not $currentPath -or -not (Test-Path -LiteralPath $currentPath)) {
        [System.Windows.Forms.MessageBox]::Show("请先指定有效的游戏安装目录！", "提示", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning)
        return
    }

    $targetMod = Join-Path $currentPath "Mods\StardewAI.Companion.Mod"
    $txtLog.Clear()
    $txtLog.AppendText(">>> 开始设置...`r`n")

    # 1. 游戏运行检查
    $proc = Get-RunningGameProcess
    if ($proc) {
        $txtLog.AppendText("提示：检测到星露谷正在运行中 (PID: $($proc.Id))。`r`n")
    }

    # 2. 安装 Mod
    try {
        $txtLog.AppendText(">>> 正在部署纯净生产 Mod 至: $targetMod ...`r`n")
        $modRes = Install-ModFiles -sourceDir $distSourceDir -destinationDir $targetMod
        $txtLog.AppendText("✔ $($modRes.Message)`r`n")
    } catch {
        $txtLog.AppendText("❌ Mod 部署遇到问题: $_`r`n")
        if ($proc) {
            $txtLog.AppendText("💡 建议：若文件被游戏进程占用，请先保存退出游戏后再尝试。`r`n")
        }
        return
    }

    # 3. 注册选定的客户端
    $chosenAgent = if ($rbAgy.Checked) { 'agy' } elseif ($rbKimi.Checked) { 'kimi' } else { 'claude' }
    try {
        $txtLog.AppendText(">>> 正在配置 AI 客户端 ($chosenAgent)...`r`n")
        $mcpRes = Register-Mcp -agentName $chosenAgent -modDir $targetMod
        $txtLog.AppendText("✔ $($mcpRes.Message)`r`n")
        Set-ChatBackendConfig -backendName $chosenAgent
    } catch {
        $txtLog.AppendText("❌ MCP 注册遇到问题: $_`r`n")
        return
    }

    # 4. 最终说明
    $txtLog.AppendText("`r`n🎉 全部设置已完成！`r`n")
    $txtLog.AppendText("使用说明：`r`n")
    $txtLog.AppendText("1. 正常运行 StardewModdingAPI.exe 进入存档（无需控制台命令，伙伴后台常驻）；`r`n")
    $txtLog.AppendText("2. 在 AI 客户端中直接发送指令（例如：'帮我种4棵防风草并浇水，缺种子去买'）；`r`n")
    $txtLog.AppendText("（MCP 服务在客户端调用时自动在后台启动，无需手动开启终端）`r`n")

    Update-UiStatus
    [System.Windows.Forms.MessageBox]::Show(
        "星露谷 AI 伙伴设置完成！`n`n后续步骤：`n1. 启动 SMAPI 并进入存档；`n2. 在 AI 客户端中直接对伙伴下达自然语言指令。`n`n（MCP 后台服务在下发指令时由客户端自动拉起，无需保持黑窗口）",
        "设置成功",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Information
    )
})

$btnViewUsage.Add_Click({
    $pyExe = Join-Path $repoRoot "runtime\.venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $pyExe)) { $pyExe = "python.exe" }
    $usageScript = Join-Path $scriptDir "view-usage.py"
    $htmlOut = Join-Path $repoRoot "artifacts\reports\usage-report.html"
    try {
        Start-Process -FilePath $pyExe -ArgumentList @($usageScript, "--html", $htmlOut) -Wait -NoNewWindow
        if (Test-Path -LiteralPath $htmlOut) {
            Start-Process $htmlOut
        }
    } catch {
        [System.Windows.Forms.MessageBox]::Show("无法启动使用记录脚本: $_", "提示", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error)
    }
})

$btnClose.Add_Click({ $form.Close() })

# 初始化刷新
Update-UiStatus
$form.ShowDialog() | Out-Null
