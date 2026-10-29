# 让自己的 AI 伙伴接入游戏

你可以继续使用已经培养好人格的那段对话。最简单的方式是给原客户端添加游戏 MCP 工具，在原聊天窗口指挥它玩；如果希望直接在游戏聊天栏交流，还需要一个能把消息送回原会话的连接适配器。

本文对应当前源码的双路径实现，尚未包含在公开的 0.3.21 下载包中。完整原会话自动接入目前支持 **MiniMax Code 0.6.2**；其他客户端可以先按下面的手动方式接入。

## 先选接入方式

| 你想怎样玩 | 接入方式 | 需要做什么 |
| --- | --- | --- |
| 在原来的 AI 聊天窗口说“帮我浇水” | 手动 MCP | 添加一个本地 MCP 服务，继续原对话 |
| 在游戏聊天栏和原来的伙伴交流，由它继续行动 | 原会话自动接入 | 启动客户端侧适配器，绑定原会话编号 |
| 从游戏里直接创建、使用一个伙伴 | 游戏内启动 | 使用已有设置向导；后续读档恢复它自己的会话 |

## 方式一：其他 Agent 简单自行接入

### 1. 准备游戏

安装匹配的 Mod 和 Runtime，通过 SMAPI 进入存档，在 F8 选择“等待已有 AI 伙伴连接”。保持游戏侧协调服务运行；手动接入时不同时运行自动会话适配器。

下文 `$mod` 都指实际安装的 `StardewAI.Companion.Mod` 文件夹，源码开发安装见[参与开发](../CONTRIBUTING.md)。

### 2. 给原客户端添加 MCP

支持本地 stdio MCP 的客户端，可以使用以下启动配置。这里采用常见的 `mcpServers` JSON 表达；具体粘贴位置、外层字段以客户端自身配置为准。如果客户端提供表单，就分别填写 command、args 和 env。

```json
{
  "mcpServers": {
    "stardew-companion": {
      "command": "C:\\你的游戏目录\\Mods\\StardewAI.Companion.Mod\\runtime\\python\\python.exe",
      "args": [
        "-B", "-m", "stardew_ai_runtime.mcp_server",
        "--run-dir", "C:\\你的游戏目录\\Mods\\StardewAI.Companion.Mod",
        "--surface", "light"
      ],
      "env": {
        "STARDEW_EXTERNAL_CODEX": "1"
      }
    }
  }
}
```

将两处路径都换成自己的 Mod 目录。`STARDEW_EXTERNAL_CODEX` 是外部手动接入开关，各客户端均使用这个名字。发行包自带 Python，无需另装。

源码环境将 command 换成 `uv`，args 换成下面的数组，env 保持相同：

```json
["run", "--project", "C:\\项目目录\\runtime", "python", "-m", "stardew_ai_runtime.mcp_server", "--run-dir", "C:\\你的游戏目录\\Mods\\StardewAI.Companion.Mod", "--surface", "light"]
```

也可用 `tools/register-mcp.ps1` 为 kimi、agy、claude、codex、dsh、mcode 打印配置。非 Codex 的外部手动接入需补上上述 `env`，并保留 `--surface light`。

Codex 可以直接安装项目级配置与技能，`-ProjectDir` 指向原对话使用的工作目录：

```powershell
$mod = "C:\你的游戏目录\Mods\StardewAI.Companion.Mod"
& "$mod\tools\register-mcp.ps1" -RunDir $mod -Agent codex -ProjectDir "C:\我的伙伴对话" -Install
```

让客户端重新加载工具；如需重启，之后恢复**原来的那段对话**。

### 3. 邀请伙伴一起玩

让客户端按其技能加载方式读取 [stardew-companion 技能](../agent-skills/stardew-companion/SKILL.md)。Codex 的上述安装脚本已复制该技能，可在原对话输入：

```text
$stardew-companion 来陪我玩星露谷吧。
```

没有技能加载功能的客户端，可以先在原对话发送：

```text
游戏已经连好了，来陪我玩星露谷吧。
```

### 4. 开始玩

接着就可以自然交流，例如“帮我把今天的作物浇好，做完一起去镇上逛逛”。

手动接入在原客户端聊天；游戏聊天栏不会自动回传消息。想直接在游戏里交流，使用下面的自动连接。

## 方式二：原会话自动连接游戏

当前可直接使用 MiniMax Code 0.6.2。保持原会话的账号、工作目录和数据目录，从原环境启动以下命令；连接期间原聊天窗口保持闲置，在游戏里继续对话。

```powershell
$mod = "C:\你的游戏目录\Mods\StardewAI.Companion.Mod"
$originalWorkspace = "C:\我的伙伴对话"
$python = Join-Path $mod "runtime\python\python.exe"

# 列出原工作目录中的已有会话，不调用模型推理。
& $python -B -m stardew_ai_runtime.external_connection --run-dir $mod --cwd $originalWorkspace --list

# 将编号替换为你要继续的那段对话，保持这个命令运行。
& $python -B -m stardew_ai_runtime.external_connection --run-dir $mod --cwd $originalWorkspace --session "原会话编号" --name "我的伙伴" --original-client-idle
```

在 F8 选择等待已有伙伴，连接成功后直接在游戏聊天栏交流。`--name` 只是游戏显示名，人格仍来自原会话；使用自定义 `MINIMAX_DATA_DIR` 时沿用原环境，或增加 `--data-dir "原数据目录"`。源码运行可用 `uv run --project runtime python` 替代 `$python -B`。

结束游玩后，按适配器窗口的 Ctrl+C，或在另一终端运行：

```powershell
& $python -B -m stardew_ai_runtime.external_connection --run-dir $mod --disconnect
```

再回到原客户端，重新打开或恢复**同一会话**以刷新记录，继续聊刚才的游戏。退出游戏会取消当轮执行，但不会关闭原客户端，也不会自动结束客户端侧适配器。

若新增工具需要信任，在原客户端处理后重连。
