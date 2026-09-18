# 星露谷物语Stardew Valley Agent

给星露谷农场添一个能听懂你说话的 AI 伙伴。

在游戏里按 `F8`，告诉它要做什么，例如“把没浇水的地浇完”或“从箱子里拿种子种下去”。模型负责理解指令、安排任务，SMAPI Mod 负责寻路和执行游戏动作；也可以通过 MCP 客户端直接调用这些能力。

项目仍在开发中。基础种植流程有实机运行记录，但自由模式、聊天界面和长时间运行还在修整，暂不作为稳定版发布。

## 目前能做什么

以下功能已经有实现，部分功能仍需继续验证，具体见下方待办。

| 能力 | 内容 |
| --- | --- |
| 查看农场 | 查询时间、天气、位置、背包、箱子、待浇地块、成熟作物、可种植位置和商店商品 |
| 种植与浇水 | 翻地、播种、浇水，可把买种或箱子取种接到种植流程中 |
| 收获与收纳 | 采收支持手采的成熟作物，向箱子存取物品、整理箱子、出货 |
| 移动与采购 | 地图内寻路、已支持路线的跨地图移动、到店购买物品 |
| 游戏内对话 | F8 中文聊天、任务状态、暂停、继续和取消；近期界面与消息修复仍待实机复核 |
| 计划与待办 | 按存档保存目标、短计划和待办，按依赖执行步骤，记录结果和等待条件 |
| 模型接入 | 游戏内聊天提供 Kimi 和 agy 后端；MCP 可供外部客户端接入 |
| 用量记录 | 记录模型请求与 Token 用量，生成本地查看页面；拿不到的用量显示为未知 |

伙伴有自己的执行状态、背包、体力和工具。农活动作受游戏条件限制：距离、障碍、材料、背包空间或商店营业情况不满足时，任务可能停下并返回原因。

## 开发中的功能与待办

- [ ] **自由模式（开发中）**：已有自主决策、短作业和玩家指令介入逻辑，需要继续验证连续干活、切换模式和中断后的行为。
- [ ] **F8 聊天与状态反馈（开发中）**：已修改文字裁剪、等待条件消息、模式确认和超时提示；真实回复、缩放、长消息滚动及断线恢复还需复核。
- [ ] **农业补充动作（开发中）**：喷壶补水、施肥、清杂物和拾取已有代码，仍需逐项核对真实消耗与执行结果。
- [ ] **畜牧与机器（开发中）**：已有动物、建筑和机器查询，以及抚摸、喂养、挤奶、剪毛、开关动物门、投料和收取成品的实现，尚未完成完整实机验收。
- [ ] **跨天记忆与持续运行（开发中）**：已有日结、待办承接和会话轮换，需要补连续三天及更长时间的实玩验证。
- [ ] **安装与发布（开发中）**：已有设置、更新和候选包检查脚本；动作执行仍依赖本地修复包清单与 DLL 校验，需移除对历史包路径的依赖，整理可从干净环境复现的发布包、升级回滚和故障诊断流程。
- [ ] **测试与兼容性（开发中）**：更新与短任务规则不一致的旧测试，补齐实例兼容校验的测试环境，处理代码规范问题，补充不同平台、农场和其他 Mod 的兼容验证。
- [ ] **跟随与返回等待（未完成）**：已有寻路基础，完整陪伴流程待实现。
- [ ] **矿洞、战斗、钓鱼、语音（未开发）**。
- [ ] **多人模式、动物购买与建筑升级、攻略知识库（暂缓）**。

睡觉和推进游戏日期仍由玩家掌握。

## 从源码构建与接入准备

目前主要在 **Windows、Stardew Valley 1.6.15、SMAPI 4.2.1** 上开发。需要自行安装游戏和 SMAPI，另备 Python 3.11～3.13、uv 和 .NET 6 SDK。游戏内对话还需要已安装并完成登录配置的 Kimi CLI 或 agy；模型服务的使用费用由所选服务决定。

**当前限制：克隆仓库后可以编译源码，但还不能仅靠下面的步骤完成农活动作。** 运行时会读取本地候选包清单并校验 Mod DLL；这些历史产物不随源码分发，缺失时会返回 `COMPATIBILITY_UNKNOWN`。可独立使用的打包与安装流程仍在开发中。

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

### 2. 安装 Mod

关闭游戏后，在游戏的 `Mods` 目录下新建 `StardewAI.Companion.Mod` 文件夹，将以下两个文件放进去：

```text
artifacts/bin/StardewAI.Companion.Mod/Release/net6.0/StardewAI.Companion.Mod.dll
src/StardewAI.Companion.Mod/manifest.json
```

建议先用备份存档体验。

根目录的设置向导需要事先准备 `artifacts/dist/StardewAI.Companion.Mod` 安装源，不能替代源码编译。完整安装包仍在开发中。

### 3. 连接模型与游戏

下面以 Kimi 为例。先完成 Kimi CLI 的安装和登录，然后复制聊天配置：

```powershell
Copy-Item config/chat-backend.example.json config/chat-backend.json
```

通过 SMAPI 启动游戏并进入存档，再注册 MCP。将下面的路径替换为实际安装位置：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/register-mcp.ps1 -RunDir "C:\你的游戏目录\Mods\StardewAI.Companion.Mod" -Agent kimi -Install
uv run --project runtime python -m stardew_ai_runtime.chat_bridge --run-dir "C:\你的游戏目录\Mods\StardewAI.Companion.Mod" --backend kimi
```

保持服务窗口开启，在游戏内按 `F8` 输入指令。其他 MCP 客户端和聊天后端的配置见 [MCP 接入指南](docs/mcp.md)。本机模型选择、账号配置和安装记录不纳入版本管理。

## 开发与检查

```powershell
uv run --project runtime ruff check runtime
uv run --project runtime pytest runtime/tests tests
uv run --project runtime python tools/check_release_boundaries.py
```

当前测试和代码规范检查仍有未通过项，主要涉及短任务规则、实例兼容校验和代码格式。贡献代码前请运行相关检查；游戏动作还需要实机验证。具体说明见[贡献指南](CONTRIBUTING.md)。

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
