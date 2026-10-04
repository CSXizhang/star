# MCP 与聊天接入

本项目通过本地 MCP 服务向模型客户端提供游戏观察和动作工具。游戏内聊天另由伙伴服务转发。发行包玩家先看[安装指南](release-guide.md)；本页保留源码开发与高级客户端的手动接入步骤。模型客户端需要自行安装和登录。

## Codex 直接接入

发行包安装完成后，在 PowerShell 运行实际安装目录里的脚本；`-ProjectDir` 指向你准备在 Codex 打开的已有文件夹：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\你的游戏目录\Mods\StardewAI.Companion.Mod\tools\register-mcp.ps1" -RunDir "C:\你的游戏目录\Mods\StardewAI.Companion.Mod" -Agent codex -ProjectDir "C:\我的农场对话" -Install
```

脚本只写该项目的 `.codex/config.toml` 和 `.agents/skills/stardew-companion/SKILL.md`，保留其他配置；已有同名非脚本配置或不同技能内容时停止并说明冲突。省略 `-ProjectDir` 则使用脚本所在发行包或仓库根目录。发行包使用自带 Python，玩家无需编译或安装开发环境。升级后可重跑注册；若旧技能不同，先保留自己的修改，再移走旧技能让脚本安装新版。

在 Codex 打开并信任这个项目，重新开启会话后输入 `$stardew-companion 看看今天农场有什么要做`。可在该目录用 `codex mcp get stardew-companion` 检查配置。项目配置仅在可信项目生效，技能按需加载，见 [Codex MCP 文档](https://developers.openai.com/codex/mcp)及[技能文档](https://developers.openai.com/codex/skills)。

通过 SMAPI 进入存档，保持自动启动的伙伴服务运行。外部 Codex 先观察状态；玩家要求执行后用 `begin_game_turn` 开启一轮，再选一个短作业，由已有执行器完成。`job-selected` 并非完成，须等待实际终态。下一业务再开启一轮；已有任务未完、结果不明、暂停或自由模式开启时会拒绝新轮次。

外部 Codex 派工前关闭自由模式、等待当前游戏内请求结束；此时不要再用 F8 或 NPC 聊天派工。不要另外启动第二个伙伴聊天服务，以免争用连接或端口。切回游戏内入口前先让外部任务结束。菜单会暂停游戏动作，关闭菜单后才继续。

两个入口使用相同游戏事实和任务记录，但**不共享同一 Codex 会话**。外部沿用当前 Codex 对话；游戏内按后端、存档和入口续接，Codex 上下文压缩由宿主管理。游戏日结算继续进行，不再仅因跨天或通用 token 阈值重开 Codex；伙伴配置、记忆或工具范围变化仍可能新建会话。

## 源码开发接入

源码开发者先按[参与开发](../CONTRIBUTING.md#从源码构建)构建和安装 Mod，通过 SMAPI 启动游戏并进入存档。Mod 会在自己的 `data` 目录下生成 `transport-discovery.json`，供 Runtime 发现本地连接。

当前动作执行依赖 Mod DLL 与本地配对清单一致：干净克隆可从源码编译后运行 `tools/setup-companion.ps1` 生成 `local-dev` 绑定完成安装；缺少清单时返回 `COMPATIBILITY_UNKNOWN`，DLL 与清单不一致时返回 `MOD_RUNTIME_MISMATCH`（重新运行设置向导同步）。

## 注册 MCP

在仓库根目录运行，将路径替换为实际 Mod 安装目录：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/register-mcp.ps1 -RunDir "C:\你的游戏目录\Mods\StardewAI.Companion.Mod" -Agent kimi -Install
```

首次使用 Kimi 时，在仓库根目录交互运行 `kimi`，核对项目信任提示中的 MCP 启动命令和 Mod 路径后确认信任。输入 `/mcp`，确认 `stardew-companion` 已连接，再启动下文的聊天服务；已打开的 Kimi 会话需重新启动才能加载新注册的工具。

`-Agent` 支持 `kimi`、`agy`、`claude`、`codex`、`dsh`、`mcode` 和 `all`。省略 `-Install` 时输出配置供你检查；不同客户端的注册方式和可用程度可能不同。

MiniMax Code 的 MCP 服务保存在数据目录的 `mcp.json`（`MINIMAX_DATA_DIR`，默认 `%USERPROFILE%\.minimax`），桌面端与 CLI 共用同一份。`-Agent mcode -Install` 会合并写入该文件、保留其他服务；如果已存在不是本脚本写入的同名条目，则停止并说明冲突，不改任何文件。

## MiniMax Code 的工具绑定方式

`mcode` 后端用 `mcode exec` 执行一轮无头任务，每轮只把游戏工具绑到这一轮：

- 它把本轮的 `.mcp.json` 写进 `<run-dir>\data\mcode\`，并用 `mcode exec --cwd` 指向该目录。**工作区里的同名条目会盖住你共享配置里的同名服务**（已实测：profile 注册的服务不再生效），所以伙伴一定连到当前这次游戏运行。
- 该文件的 `env` 承载本轮授权（`STARDEW_DECISION_TOKEN`、surface、life 模式字段）。`--config` 里的 `mcpServers` **不生效**，工具只来自工作区文件与数据目录的 `mcp.json`。
- 不传 `--config`，因此本机 `~/.minimax` 下的模型选择、登录态与凭据都原样沿用，不读也不改。留空 `model` 即用本机默认模型；默认 `--effort low`，可选 `default/low/medium/high/xhigh/max`，档位必须被所选模型支持，否则 CLI 在本轮开始前就以退出码 2 报错。
- CLI 从 PATH 解析；PowerShell 执行策略拦下 npm 的 `.ps1` 垫片时，后端自动改用同名 `.cmd`。会话用 `--session` 续跑，恢复失败会改开一个新会话，不会把农场流程卡死。
- 注意：工作区条目是**追加**的，你共享配置里其他 MCP 服务在这一轮仍然可见；`--permission full` 也不拦终端与文件工具，行为与 Codex 后端的 `danger-full-access` 一致，靠运行时指令约束模型只用游戏工具。

在向导中选择 `mcode` 即可（需先装好并登录 MiniMax Code CLI）。同一台机器上也可以用 `register-mcp.ps1 -Agent mcode` 把游戏工具注册给 MiniMax Code 自己作为外部客户端；两者互不影响。

手动配置支持 stdio 的 MCP 客户端时，可以直接使用 `uv run --project <仓库根目录>/runtime python`（与 `register-mcp.ps1 -Install` 生成的配置一致），参数为：

```text
-m stardew_ai_runtime.mcp_server --run-dir <Mod 安装目录>
```

也可以使用仓库虚拟环境中的 Python（通常位于 `runtime/.venv/Scripts/python.exe`），效果相同。请使用绝对路径，工作目录设为仓库根目录。

## 游戏内聊天

复制 `config/chat-backend.example.json` 为 `config/chat-backend.json`，设置 `backend` 和 `model`。当前聊天后端支持 `kimi`、`agy`、`codex`、`dsh` 和 `mcode`。使用本机已登录的 Codex CLI 时，设置 `"backend": "codex"`；省略 `model` 会沿用本机 Codex 配置的模型。每次调用只临时绑定当前游戏的 MCP，不修改全局配置。

```powershell
uv run --project runtime python -m stardew_ai_runtime.chat_bridge --run-dir "C:\你的游戏目录\Mods\StardewAI.Companion.Mod" --backend kimi
```

保持服务运行，在游戏中按 `F8` 打开对话。发送指令后窗口会关闭，让游戏动作继续；再次按 `F8` 可查看同一条指令的后续进度和完整回复。打开菜单会暂停农活动作的推进，关闭菜单后才会继续执行。

生活菜单、记忆、节点规划，以及暂停、取消与暂缓计划的区别，见[玩家指南](companion-guide.md)。

## 常见问题

- **模型不支持思考强度参数**：若 agy 报 `--effort is not supported for model`，在本次聊天服务的启动参数中设置 `--effort default`，让桥接省略该选项、使用模型自身默认值；不改变其他会话或全局模型配置。

- **找不到游戏连接**：确认已通过 SMAPI 进入存档，`--run-dir` 指向实际加载的 Mod 目录。
- **模型没有游戏工具**：检查当前客户端是否加载了本项目的 MCP 配置，以及 Python 路径是否正确。
- **有回复但没有执行动作**：查看任务结果和错误原因；文本回复不等于动作完成。也可能是背包、材料、位置或版本校验不满足条件。
- **用量显示未知**：所选后端未提供可归属的统计信息，不能视为零消耗。

不要公开账号凭据、`transport-discovery.json` 中的会话令牌或未处理的完整聊天记录。
