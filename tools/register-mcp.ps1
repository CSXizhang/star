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
    [ValidateSet('agy', 'kimi', 'claude', 'codex', 'dsh', 'all')][string]$Agent = 'all',
    [switch]$Install
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

function Format-CliArg([string]$arg) {
    if ($arg -match '[\s"''`$]') {
        return '"' + ($arg -replace '"', '\"') + '"'
    }
    return $arg
}

$formattedArgs = ($uvArgs | ForEach-Object { Format-CliArg $_ }) -join ' '

function Show-Agy {
    Write-Host "`n=== agy ==="
    Write-Host "agy mcp add $serverName -- uv $formattedArgs"
    Write-Host "（agy 客户端在接收自然语言指令时会自动启动后台 MCP 进程）"
}

function Show-Kimi {
    Write-Host "`n=== Kimi Code (.kimi-code/mcp.json，项目级) ==="
    $doc = @{ mcpServers = @{ $serverName = @{ command = "uv"; args = $uvArgs } } }
    Write-Host ($doc | ConvertTo-Json -Depth 5)
}

function Show-Claude {
    Write-Host "`n=== Claude Desktop (claude_desktop_config.json) ==="
    $doc = @{ mcpServers = @{ $serverName = @{ command = "uv"; args = $uvArgs } } }
    Write-Host ($doc | ConvertTo-Json -Depth 5)
}

function Show-Codex {
    Write-Host "`n=== Codex (~/.codex/config.toml) ==="
    Write-Host "[mcp_servers.$serverName]"
    Write-Host 'command = "uv"'
    $tomlArgs = ($uvArgs | ForEach-Object { '"' + ($_ -replace '\\', '\\') + '"' }) -join ", "
    Write-Host "args = [ $tomlArgs ]"
}

function Show-Dsh {
    Write-Host "`n=== dsh / 其它 MCP stdio 客户端 ==="
    Write-Host "command: uv"
    Write-Host "args:    $formattedArgs"
    Write-Host "按该客户端的 MCP stdio server 配置格式填入即可。"
}

function Install-Agy {
    Write-Host ">>> 注册进 agy..."
    $configured = & agy mcp list 2>&1 | Out-String
    if ($configured -match $serverName) {
        & agy mcp remove $serverName 2>&1 | Out-Null
    }
    & agy mcp add $serverName -- uv @uvArgs
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
                    $serverMap[$prop.Name] = @{
                        command = [string]$prop.Value.command
                        args = @($prop.Value.args | ForEach-Object { [string]$_ })
                    }
                }
            }
        } catch {
            Write-Warning "读取已有 $mcpFile 失败，将新建: $_"
        }
    }
    $serverMap[$serverName] = @{ command = "uv"; args = $uvArgs }
    $config = @{ mcpServers = $serverMap }
    ($config | ConvertTo-Json -Depth 10) | Set-Content -LiteralPath $mcpFile -Encoding utf8
    Write-Host ">>> 已写入 $mcpFile（重启客户端或新会话生效）"
    Write-Host ">>> From the repo root, run interactive kimi, review and trust this folder, then use /mcp to verify $serverName before starting the chat bridge."
}

switch ($Agent) {
    'agy'    { Show-Agy;    if ($Install) { Install-Agy } }
    'kimi'   { Show-Kimi;   if ($Install) { Install-Kimi } }
    'claude' { Show-Claude; if ($Install) { Write-Warning "Claude Desktop 请将上述 JSON 片段合并至 claude_desktop_config.json" } }
    'codex'  { Show-Codex;  if ($Install) { Write-Warning "Codex 请将上述 TOML 片段合并至 ~/.codex/config.toml" } }
    'dsh'    { Show-Dsh;    if ($Install) { Write-Warning "dsh 请按其文档填入 command 与 args" } }
    'all'    { Show-Agy; Show-Kimi; Show-Claude; Show-Codex; Show-Dsh }
}
