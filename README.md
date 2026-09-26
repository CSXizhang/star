# 星露谷 AI 伙伴

给星露谷农场添一个能听懂你说话、一起照料农场的 AI 伙伴。

Powered by Astra, Kimi K3, Gemini 3.8-flash, DeepSeek V4.1-flash.

## 能做什么

| 功能 | 内容 |
| --- | --- |
| 农场打理 | 翻地、播种、浇水、施肥、收获、清杂物、伐木与采集 |
| 物品与采购 | 查看背包和箱子、存取与整理物品、出货、到店购买 |
| 动物与机器 | 喂养、抚摸、挤奶、剪毛、开关动物门，机器投料与收取成品 |
| 自由模式 | 按你选择的农活方向照料农场，遵守每日购买上限 |
| 对话与控制 | 游戏内中文对话、工作进度、暂停、继续和取消 |
| 伙伴生活 | 初次见面由你起名、选择相处方式，闲聊、偏好与约定记忆、共同经历和伙伴消息 |
| 模型接入 | Kimi、agy 聊天后端，以及供其他客户端使用的 MCP 工具 |

在游戏里按 **F8**，可以直接说：

> “把没浇水的菜浇完。”
>
> “从箱子里拿防风草种子，种到空地上。”

走近伙伴按交互键，就能通过原生头像对话认识彼此：第一次给伙伴起个名字，以后聊近况、商量安排或查看约定。详细用法见[玩家指南](docs/companion-guide.md)。

## 开始使用

目前以 **Windows、Stardew Valley 1.6.15、SMAPI 4.2.1** 为开发环境，从源码构建安装。需要游戏与 SMAPI、Python 3.11～3.13、[uv](https://docs.astral.sh/uv/)、.NET 6 SDK，以及已安装并登录的 Kimi CLI 或 agy。

### 1. 准备与构建

克隆仓库后，在仓库根目录打开 PowerShell：

```powershell
Copy-Item .env.example .env.local
uv sync --project runtime --locked
```

编辑 `.env.local`，填写本机游戏与 SMAPI 路径。没有 .NET SDK 时先运行 `tools/bootstrap-dotnet.ps1`，然后检查环境并构建：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/preflight.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/build-mod.ps1 -Configuration Release
```

### 2. 安装伙伴

关闭游戏，运行设置向导完成 Mod 安装与本地配对：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/setup-companion.ps1 -AutoInstall
```

也可以双击 `设置星露谷伙伴.bat`。以后重新构建 Mod，再运行一次向导同步安装与配对即可。

### 3. 配置模型

以下以 Kimi 为例。完成 CLI 登录后，复制模型配置，按所用账号调整其中的模型名称：

```powershell
Copy-Item config/chat-backend.example.json config/chat-backend.json
```

通过 **SMAPI 启动游戏并进入存档**，再注册工具。把下面的路径替换为实际 Mod 安装目录：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/register-mcp.ps1 -RunDir "C:\你的游戏目录\Mods\StardewAI.Companion.Mod" -Agent kimi -Install
```

首次使用，在仓库根目录运行 `kimi`，核对 MCP 启动命令和 Mod 路径后确认项目信任。输入 `/mcp`，确认 `stardew-companion` 已连接，然后退出这个检查会话。

### 4. 启动服务

```powershell
uv run --project runtime python -m stardew_ai_runtime.chat_bridge --run-dir "C:\你的游戏目录\Mods\StardewAI.Companion.Mod" --backend kimi
```

保持服务窗口开启，回到游戏走近伙伴按交互键，第一次见面时给她起个名字；按 **F8** 可以交代工作。agy 和其他客户端的配置、安装细节与故障排查见[接入指南](docs/mcp.md)。

## 正在开发

- 更自然的“看现状、商量方案、一句认可后接手准备”交互，以及节日与季节节点规划、Wiki 依据查询。
- 更长时间的跨天陪伴、界面与断线恢复体验。
- 正式发布包、升级流程和更多环境兼容。
- 跟随陪伴；矿洞、战斗、钓鱼和语音等新能力。

## 更多文档

- [玩家指南](docs/companion-guide.md)：生活菜单、自由模式、记忆、规划与工作控制。
- [模型与 MCP 接入](docs/mcp.md)：模型配置、配对、连接和故障排查。
- [参与开发](CONTRIBUTING.md)：代码结构、构建检查和贡献方式。
- [通信协议](protocol/README.md)：消息与工具接入约定。

## 许可证

代码采用 [Apache License 2.0](LICENSE)。本项目为非官方项目；Stardew Valley、SMAPI 及其他依赖的名称、资源和权利归各自权利人所有。
