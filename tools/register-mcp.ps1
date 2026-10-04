# Register the Stardew AI Companion MCP server into various agent CLIs, or print
# ready-to-paste configs. The MCP server connects to a RUNNING game session via
# its transport-discovery.json, so the game must be started first (normal play).
#
# Usage:
#   tools/register-mcp.ps1 -RunDir <run目录>                  # 打印所有 agent 的配置片段
#   tools/register-mcp.ps1 -RunDir <run目录> -Agent agy -Install  # 直接注册进 agy
#   tools/register-mcp.ps1 -RunDir <run目录> -Agent kimi -Install # 写入项目 .kimi-code/mcp.json
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RunDir,
    [ValidateSet('agy', 'kimi', 'claude', 'codex', 'dsh', 'mcode', 'all')][string]$Agent = 'all',
    [switch]$Install,
    [string]$ProjectDir
)

$ErrorActionPreference = 'Stop'
# The double-click/automation callers often decode PowerShell output with the
# active legacy Windows code page. Keep diagnostics byte-safe; JSON/config
# content remains UTF-8 when written to files.
$OutputEncoding = [System.Text.Encoding]::ASCII
[Console]::OutputEncoding = [System.Text.Encoding]::ASCII

# Dynamically resolve repo root from script location
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
if (-not $ProjectDir) { $ProjectDir = $repoRoot }

$resolved = Resolve-Path -LiteralPath $RunDir -ErrorAction SilentlyContinue
if (-not $resolved) { $resolved = Join-Path $repoRoot $RunDir }
if (-not (Test-Path -LiteralPath $resolved)) { throw "RunDir not found: $RunDir" }
$runDirAbs = (Resolve-Path -LiteralPath $resolved).Path

# Check for transport-discovery.json in common locations (Mod dir or Game/Run dir)
$discoveryCandidates = @(
    (Join-Path $runDirAbs "data\transport-discovery.json"),
    (Join-Path $runDirAbs "mods\StardewAI.Companion.Mod\data\transport-discovery.json"),
    (Join-Path $runDirAbs "Mods\StardewAI.Companion.Mod\data\transport-discovery.json")
)
$discoveryFound = $false
foreach ($cand in $discoveryCandidates) {
    if (Test-Path -LiteralPath $cand) {
        $discoveryFound = $true
        break
    }
}
if (-not $discoveryFound) {
    Write-Warning "transport-discovery.json 尚未在 $runDirAbs 生成。`n提示：游戏启动并载入存档后会自动发布 discovery；注册可先行完成。"
}

$serverName = "stardew-companion"
$uvArgs = @("run", "--project", (Join-Path $repoRoot "runtime"), "python", "-m", "stardew_ai_runtime.mcp_server", "--run-dir", $runDirAbs, "--surface", "light")
$mcpCommand = 'uv'
if (Test-Path -LiteralPath (Join-Path $repoRoot 'release-manifest.json')) {
    . (Join-Path $scriptDir 'release-package.ps1')
    $release = Read-VerifiedRelease $repoRoot
    if ($runDirAbs.TrimEnd('\') -ne $repoRoot.TrimEnd('\')) { throw 'Release MCP must bind to its own installed Mod directory.' }
    $mcpCommand = Get-ReleasePath $repoRoot $release.python
    $uvArgs = @('-B', '-m', 'stardew_ai_runtime.mcp_server', '--run-dir', $runDirAbs, '--surface', 'light')
}

function Format-CliArg([string]$arg) {
    if ($arg -match '[\s"''`$]') {
        return '"' + ($arg -replace '"', '\"') + '"'
    }
    return $arg
}

$formattedArgs = ($uvArgs | ForEach-Object { Format-CliArg $_ }) -join ' '

function Show-Agy {
    Write-Host "`n=== agy ==="
    Write-Host "agy mcp add $serverName -- $(Format-CliArg $mcpCommand) $formattedArgs"
    Write-Host "（agy 客户端在接收自然语言指令时会自动启动后台 MCP 进程）"
}

function Show-Kimi {
    Write-Host "`n=== Kimi Code (.kimi-code/mcp.json，项目级) ==="
    $doc = @{ mcpServers = @{ $serverName = @{ command = $mcpCommand; args = $uvArgs } } }
    Write-Host ($doc | ConvertTo-Json -Depth 5)
}

function Show-Claude {
    Write-Host "`n=== Claude Desktop (claude_desktop_config.json) ==="
    $doc = @{ mcpServers = @{ $serverName = @{ command = $mcpCommand; args = $uvArgs } } }
    Write-Host ($doc | ConvertTo-Json -Depth 5)
}

function Show-Codex {
    Write-Host "`n=== Codex (project .codex/config.toml) ==="
    Write-Host (Get-CodexConfig)
}

function Get-CodexConfig {
    # JSON strings/arrays are also valid TOML basic strings/arrays.
    $commandJson = ConvertTo-Json -InputObject $mcpCommand -Compress
    $argsJson = ConvertTo-Json -InputObject @($uvArgs) -Compress
    return "[mcp_servers.$serverName]`ncommand = $commandJson`nargs = $argsJson`nenv = { STARDEW_EXTERNAL_CODEX = `"1`" }`n"
}

function Install-Codex {
    $project = (Resolve-Path -LiteralPath $ProjectDir).Path
    $configDir = Join-Path $project '.codex'
    $configFile = Join-Path $configDir 'config.toml'
    $skillSource = Join-Path $repoRoot 'agent-skills/stardew-companion/SKILL.md'
    $skillDir = Join-Path $project '.agents/skills/stardew-companion'
    $skillFile = Join-Path $skillDir 'SKILL.md'
    $begin = '# BEGIN stardew-companion (register-mcp.ps1)'
    $end = '# END stardew-companion (register-mcp.ps1)'
    $content = if (Test-Path -LiteralPath $configFile) { [IO.File]::ReadAllText($configFile) } else { '' }
    $pattern = '(?ms)^' + [regex]::Escape($begin) + '\r?\n.*?^' + [regex]::Escape($end) + '(?:\r?\n|$)'
    $remaining = [regex]::Replace($content, $pattern, '')
    if ($remaining.Contains($begin) -or $remaining.Contains($end) -or $remaining -match '(?m)^\s*\[\s*mcp_servers\.(?:stardew-companion|"stardew-companion"|''stardew-companion'')\s*[.\]]') {
        throw 'Existing stardew-companion configuration is not script-managed; review it before installing. No files changed.'
    }
    $skill = [IO.File]::ReadAllText($skillSource)
    if ((Test-Path -LiteralPath $skillFile) -and [IO.File]::ReadAllText($skillFile) -ne $skill) {
        throw 'Existing stardew-companion skill differs; preserve or remove it before installing. No files changed.'
    }
    $block = "$begin`n$(Get-CodexConfig)$end`n"
    $updated = if ($content -match $pattern) { [regex]::Replace($content, $pattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($match) $block }) } else { $content + "`n" + $block }
    New-Item -ItemType Directory -Path $configDir, $skillDir -Force | Out-Null
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($configFile, $updated, $utf8)
    [IO.File]::WriteAllText($skillFile, $skill, $utf8)
    Write-Host ">>> Installed project MCP and skill in $project. No global configuration changed."
    Write-Host '>>> Open/trust this project in Codex, restart its session, then use $stardew-companion.'
}

function Show-Dsh {
    Write-Host "`n=== dsh / 其它 MCP stdio 客户端 ==="
    Write-Host "command: $mcpCommand"
    Write-Host "args:    $formattedArgs"
    Write-Host "按该客户端的 MCP stdio server 配置格式填入即可。"
}

function Get-McodeConfig {
    $doc = @{ mcpServers = @{ $serverName = @{ command = $mcpCommand; args = $uvArgs } } }
    return ($doc | ConvertTo-Json -Depth 5)
}

function Show-Mcode {
    Write-Host "`n=== MiniMax Code (mcode) ==="
    Write-Host "MiniMax Code 的 MCP 服务保存在数据目录 mcp.json（默认 %USERPROFILE%\.minimax\mcp.json），"
    Write-Host "供桌面端与 CLI 共用；这份配置只影响外部客户端的对话，不影响游戏内聊天后端。"
    Write-Host "也可直接在 MiniMax Code 中用内置 MCP 工具注册 stdio 服务，参数如下："
    Write-Host (Get-McodeConfig)
}

function Install-Mcode {
    $mcodeHome = if ($env:MINIMAX_DATA_DIR) { $env:MINIMAX_DATA_DIR } elseif ($env:MAVIS_DATA_DIR) { $env:MAVIS_DATA_DIR } else { Join-Path $env:USERPROFILE ".minimax" }
    $mcpFile = Join-Path $mcodeHome "mcp.json"
    $serverMap = @{}
    if (Test-Path -LiteralPath $mcpFile) {
        try {
            $raw = Get-Content -LiteralPath $mcpFile -Raw -Encoding utf8
            $parsed = ConvertFrom-Json -InputObject $raw
            if ($parsed.mcpServers) {
                foreach ($prop in $parsed.mcpServers.PSObject.Properties) {
                    $serverMap[$prop.Name] = $prop.Value
                }
            }
            if ($serverMap.ContainsKey($serverName)) {
                throw "Existing $serverName entry in $mcpFile was not created by this script; review it before installing. No files changed."
            }
        } catch {
            throw "Could not read or safely merge $mcpFile ($_). No files changed."
        }
    }
    $serverMap[$serverName] = @{ command = $mcpCommand; args = $uvArgs }
    New-Item -ItemType Directory -Path $mcodeHome -Force | Out-Null
    $config = @{ mcpServers = $serverMap }
    [System.IO.File]::WriteAllText($mcpFile, ($config | ConvertTo-Json -Depth 10), (New-Object System.Text.UTF8Encoding($false)))
    Write-Host ">>> 已写入 $mcpFile（重启 MiniMax Code 或新会话生效）"
}

function Install-Agy {
    Write-Host ">>> 注册进 agy..."
    $configured = & agy mcp list 2>&1 | Out-String
    if ($configured -match $serverName) {
        & agy mcp remove $serverName 2>&1 | Out-Null
    }
    & agy mcp add $serverName -- $mcpCommand @uvArgs
    if ($LASTEXITCODE -ne 0) { throw 'agy MCP registration failed.' }
    & agy mcp list
}

function Install-Kimi {
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
                    $serverMap[$prop.Name] = $prop.Value
                }
            }
        } catch {
            if ($release) { throw 'Existing project MCP configuration is invalid; repair it before setup. It was not replaced.' }
            Write-Warning "读取已有 $mcpFile 失败，将新建: $_"
        }
    }
    $serverMap[$serverName] = @{ command = $mcpCommand; args = $uvArgs }
    $config = @{ mcpServers = $serverMap }
    ($config | ConvertTo-Json -Depth 10) | Set-Content -LiteralPath $mcpFile -Encoding utf8
    Write-Host ">>> 已写入 $mcpFile（重启客户端或新会话生效）"
    Write-Host ">>> From the repo root, run interactive kimi, review and trust this folder, then use /mcp to verify $serverName before starting the chat bridge."
}

switch ($Agent) {
    'agy'    { Show-Agy;    if ($Install) { Install-Agy } }
    'kimi'   { Show-Kimi;   if ($Install) { Install-Kimi } }
    'claude' { Show-Claude; if ($Install) { Write-Warning "Claude Desktop 请将上述 JSON 片段合并至 claude_desktop_config.json" } }
    'codex'  { Show-Codex;  if ($Install) { Install-Codex } }
    'dsh'    { Show-Dsh;    if ($Install) { Write-Warning "dsh 请按其文档填入 command 与 args" } }
    'mcode'  { Show-Mcode;  if ($Install) { Install-Mcode } }
    'all'    { Show-Agy; Show-Kimi; Show-Claude; Show-Codex; Show-Dsh; Show-Mcode }
}
