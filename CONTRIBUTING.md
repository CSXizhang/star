# 参与开发

欢迎提交问题和修复。当前项目主要在 Windows 上开发，游戏版本基线为 Stardew Valley 1.6.15、SMAPI 4.2.1。玩家安装请看[首页](README.md)，以下步骤面向源码开发。

## 从源码构建

需要游戏与 SMAPI、Python 3.11～3.13、uv、.NET 6 SDK，以及已安装并登录的 Kimi CLI 或 agy。在克隆的仓库根目录打开 PowerShell：

```powershell
Copy-Item .env.example .env.local
uv sync --project runtime --locked
```

编辑 `.env.local` 填写游戏与 SMAPI 路径。没有 .NET SDK 时先运行 `tools/bootstrap-dotnet.ps1`，然后检查、构建并安装：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/preflight.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/build-mod.ps1 -Configuration Release
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/setup-companion.ps1 -AutoInstall
```

安装时关闭游戏。重新构建后再运行设置向导，同步 DLL 与本地配对清单。开发环境的模型配置、MCP 注册和手动启动命令见[接入指南](docs/mcp.md)。

## 代码结构

- `src/StardewAI.Companion.Mod`：游戏状态观察、寻路、动作执行、聊天界面及本地通信。
- `runtime/src/stardew_ai_runtime`：MCP 工具、模型后端、计划执行、工作记忆与用量统计。
- `protocol`：消息结构和协议样例；协议仍可能调整。
- `src/*Tests`、`runtime/tests`、`tests`：单元测试与接入测试。

游戏对象的读写在游戏主线程执行。模型发出任务意图，具体动作由 Mod 执行；修改动作逻辑时需要同时检查前置条件、物品消耗和最终结果。

## 验证改动

在仓库根目录运行：

```powershell
uv sync --project runtime --locked
uv run --project runtime ruff check runtime
uv run --project runtime pytest runtime/tests tests
uv run --project runtime python tools/check_release_boundaries.py
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/build-mod.ps1 -Configuration Release
```

C# 测试需要本机游戏程序集和 .NET SDK。可以使用 `dotnet test` 分别运行 `src` 下的测试项目，并通过 `StardewGamePath` 构建参数指定游戏目录。

测试与代码规范基线已全绿（lint + pytest）；依赖游戏实例或 Windows PowerShell 的用例在无对应环境时按条件跳过。请说明改动前后的相关测试结果，不要用新增跳过来掩盖回归。

离线测试不应调用付费模型服务。涉及游戏动作的验证请使用备份存档，记录实际消耗与结果，不要仅凭模型回复判断成功。

## 提交问题和修改

问题报告请包含游戏、SMAPI 和模型客户端版本、复现步骤、预期结果及实际结果。日志应去掉账号、令牌、私人路径和无关聊天内容。

提交修改时说明解决了什么问题、如何验证，以及仍有哪些限制。源码使用 Apache-2.0 许可证；不要提交游戏程序集、存档、模型凭据或本地运行产物。
