# 星露谷物语Stardew Valley Agent

给星露谷农场添一个能听懂你说话的 AI 伙伴。

在游戏里按 `F8`，告诉它要做什么，例如“把没浇水的地浇完”或“从箱子里拿种子种下去”。模型负责理解指令、安排任务，SMAPI Mod 负责寻路和执行游戏动作；也可以通过 MCP 客户端直接调用这些能力。

项目仍在开发中。种植流程、组合日常作业、跨天日结与连续三天自由模式均有实机验收记录；聊天界面细节与更长时间的稳定性仍在修整，暂不作为稳定版发布。

## 目前能做什么

以下功能已经有实现，部分功能仍需继续验证，具体见下方待办。

| 能力 | 内容 |
| --- | --- |
| 查看农场 | 查询时间、天气、位置、背包、箱子、待浇地块、成熟作物、可种植位置和商店商品 |
| 种植与浇水 | 翻地、播种、浇水，可把买种或箱子取种接到种植流程中 |
| 收获与收纳 | 采收支持手采的成熟作物，向箱子存取物品、整理箱子、出货 |
| 移动与采购 | 地图内寻路、已支持路线的跨地图移动、到店购买物品 |
| 林业与采集 | 斧砍野生树、巨树桩与空心木（多 tick 砍倒并清桩），捡拾地面掉落物与野生采集品 |
| 畜牧与机器 | 抚摸、喂养、挤奶、剪毛、开关动物门；机器投料与收取成品 |
| 游戏内对话 | F8 中文聊天、任务状态、暂停、继续和取消；近期界面与消息修复仍待实机复核 |
| 计划与待办 | 按存档保存目标、短计划和待办，按依赖执行步骤，记录结果和等待条件 |
| 模型接入 | 游戏内聊天提供 Kimi 和 agy 后端；MCP 可供外部客户端接入 |
| 用量记录 | 记录模型请求与 Token 用量，生成本地查看页面；拿不到的用量显示为未知 |

伙伴有自己的执行状态、背包、体力和工具。农活动作受游戏条件限制：距离、障碍、材料、背包空间或商店营业情况不满足时，任务可能停下并返回原因。

## 开发中的功能与待办

- [x] **自由模式（已实机验收）**：自主决策、短作业、失败熔断冷却（连续失败自动暂停重试）和玩家指令介入已实现并通过隔离实机验收；连续三天自由模式实跑通过（r52-3dayfree-20260924-02，12/12：26 轮自主决策，覆盖浇水、耕地、播种、清杂物、拾取、喷壶补水和买种子，3 次原生晕倒换日并完成日结）。
- [ ] **F8 聊天与状态反馈（开发中）**：多业务指令会自动续链推进到完成或明确收尾；已修改文字裁剪、等待条件消息、模式确认、暂停状态显示和超时提示；真实回复、缩放、长消息滚动及断线恢复还需复核。
- [x] **农业补充动作（已实机验收）**：喷壶补水、施肥、清杂物、拾取与伐木已通过隔离实机验收（r51-farmcap-20260923-15，24/24，真实原生状态与体力消耗核对）。
- [x] **组合日常流（已实机验收）**：一条指令内的浇水→收获→出货多任务自动续链（1 轮玩家请求 + 多轮自动续链，全部完成）已通过隔离实机验收（r52-dailychain-20260924-06，10/10，浇水、收获、出货真实原生效果与体力消耗核对）。
- [x] **畜牧与机器（已实机验收）**：动物、建筑和机器查询，以及抚摸、喂养、挤奶、剪毛、开关动物门、投料和收取成品，同批隔离实机验收通过。
- [ ] **跨天记忆与持续运行（开发中）**：日结、待办承接和会话轮换已实现，单日跨天链路（当日短作业 → 明日待办登记 → 原生晕倒换日 → 日结归档 → 待办到期 → 次日执行）已通过隔离实机验收（r53-dayroll-20260924-08，17/17）；连续三天自由模式实跑（含 3 次换日日结）已通过（r52-3dayfree-20260924-02，12/12）。
- [ ] **安装与发布（开发中）**：源码编译 + 设置向导自建 `local-dev` 配对已可用；正式的版本化发布包、升级回滚和故障诊断流程仍在整理。
- [ ] **测试与兼容性（开发中）**：更新与短任务规则不一致的旧测试，补齐实例兼容校验的测试环境，处理代码规范问题，补充不同平台、农场和其他 Mod 的兼容验证。
- [ ] **跟随与返回等待（未完成）**：已有寻路基础，完整陪伴流程待实现。
- [ ] **矿洞、战斗、钓鱼、语音（未开发）**。
- [ ] **多人模式、动物购买与建筑升级、攻略知识库（暂缓）**。

睡觉和推进游戏日期仍由玩家掌握。

## 从源码构建与接入准备

目前主要在 **Windows、Stardew Valley 1.6.15、SMAPI 4.2.1** 上开发。需要自行安装游戏和 SMAPI，另备 Python 3.11～3.13、uv 和 .NET 6 SDK。游戏内对话还需要已安装并完成登录配置的 Kimi CLI 或 agy；模型服务的使用费用由所选服务决定。

源码编译、安装与自建配对（`local-dev` 清单）已经可以从干净克隆完成：按下文编译后运行 `tools/setup-companion.ps1` 即可动作执行。校验失败时仍会返回 `COMPATIBILITY_UNKNOWN`（未绑定）或 `MOD_RUNTIME_MISMATCH`（DLL 与清单不一致，重新运行设置向导即可同步）。

### 1. 准备环境并编译

在仓库根目录打开 PowerShell：

```powershell
Copy-Item .env.example .env.local
# 编辑 .env.local，填写本机游戏和 SMAPI 路径。
uv sync --project runtime --locked
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/bootstrap-dotnet.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/preflight.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/build-mod.ps1 -Configuration Release
```

已有 .NET SDK 时可以跳过 `bootstrap-dotnet.ps1`。编译依赖本机游戏程序集，仓库不附带游戏文件、存档或已编译的 Mod。

### 2. 安装 Mod 并完成绑定

推荐直接使用设置向导，一次完成安装与配对绑定：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/setup-companion.ps1 -AutoInstall
```

`tools/build-mod.ps1` 编译后已把产物同步到 `artifacts/dist/StardewAI.Companion.Mod`；向导完成安装并生成本地 `local-dev` 配对清单（`artifacts/releases/local-dev/manifest.json`），运行时的 DLL 校验即可通过。也可以双击 `设置星露谷伙伴.bat` 使用图形向导。之后若重新编译了 Mod，重跑一次设置向导即可同步绑定。

**注意：只手动复制 DLL 而不运行向导，动作执行会被 `COMPATIBILITY_UNKNOWN` 拦截**——运行时要求 Mod 目录与配对清单一致。坚持手动安装时，关闭游戏后在游戏 `Mods` 目录下新建 `StardewAI.Companion.Mod` 文件夹，放入编译产物（`StardewAI.Companion.Mod.dll` 及同目录附属文件、`src/StardewAI.Companion.Mod/manifest.json`），然后仍需运行一次设置向导生成配对清单。

建议先用备份存档体验。

### 3. 连接模型与游戏

下面以 Kimi 为例。先完成 Kimi CLI 的安装和登录，然后复制聊天配置：

```powershell
Copy-Item config/chat-backend.example.json config/chat-backend.json
```

通过 SMAPI 启动游戏并进入存档，再注册 MCP。将下面的路径替换为实际安装位置：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/register-mcp.ps1 -RunDir "C:\你的游戏目录\Mods\StardewAI.Companion.Mod" -Agent kimi -Install
```

首次使用时，在仓库根目录运行 `kimi` 打开交互界面，核对项目信任提示中的 MCP 启动命令和 Mod 路径，再确认信任此目录。输入 `/mcp`，确认 `stardew-companion` 已连接；注册后已打开的 Kimi 会话需重新启动。完成后再启动聊天服务：

```powershell
uv run --project runtime python -m stardew_ai_runtime.chat_bridge --run-dir "C:\你的游戏目录\Mods\StardewAI.Companion.Mod" --backend kimi
```

保持服务窗口开启，在游戏内按 `F8` 输入指令（也可以双击 `启动伙伴服务.bat` 启动服务、`查看使用记录.bat` 查看本地用量记录）。其他 MCP 客户端和聊天后端的配置见 [MCP 接入指南](docs/mcp.md)。本机模型选择、账号配置和安装记录不纳入版本管理。当前不提供预编译发布包，请按上文从源码构建。

## 开发与检查

```powershell
uv run --project runtime ruff check runtime
uv run --project runtime pytest runtime/tests tests
uv run --project runtime python tools/check_release_boundaries.py
```

测试与代码规范基线已全绿；依赖游戏实例或 Windows PowerShell 的用例在无对应环境时按条件跳过。贡献代码前请运行相关检查；游戏动作还需要实机验证。具体说明见[贡献指南](CONTRIBUTING.md)。

| 目录 | 内容 |
| --- | --- |
| `src/StardewAI.Companion.Mod` | SMAPI Mod、伙伴动作、观察、寻路、F8 界面和本地通信 |
| `runtime` | Python Runtime、MCP 工具、聊天服务、计划执行和记忆 |
| `protocol` | 通信协议与消息样例 |
| `src/*Tests`、`runtime/tests`、`tests` | C#、Python 和脚本测试 |
| `tools` | 构建、接入、安装及用量查看脚本 |
| `docs` | 使用与接入说明 |

反馈问题时，请附上游戏与 SMAPI 版本、复现步骤，以及去掉账号信息后的相关日志。欢迎提交修复；涉及游戏动作的改动，请说明是否经过实机验证。

## 许可证

本项目代码采用 [Apache License 2.0](LICENSE)。Stardew Valley、SMAPI 及其他依赖的名称、资源和权利归各自权利人所有。本项目为非官方项目。
