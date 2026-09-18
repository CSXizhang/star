# MCP 与聊天接入

本项目通过本地 MCP 服务向模型客户端提供游戏观察和动作工具。游戏内聊天另由 Python 聊天服务转发。模型客户端需要自行安装和登录。

## 接入前

先按[首页](../README.md)编译和安装 Mod，通过 SMAPI 启动游戏并进入存档。Mod 会在自己的 `data` 目录下生成 `transport-discovery.json`，供 Runtime 发现本地连接。

当前动作执行还依赖配套包清单与 DLL 校验。干净克隆尚不能完成完整安装；缺少清单时返回 `COMPATIBILITY_UNKNOWN`，版本不一致时返回 `MOD_RUNTIME_MISMATCH`。接入成功不代表已满足动作执行条件。

## 注册 MCP

在仓库根目录运行，将路径替换为实际 Mod 安装目录：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/register-mcp.ps1 -RunDir "C:\你的游戏目录\Mods\StardewAI.Companion.Mod" -Agent kimi -Install
```

`-Agent` 支持 `kimi`、`agy`、`claude`、`codex`、`dsh` 和 `all`。省略 `-Install` 时输出配置供你检查；不同客户端的注册方式和可用程度可能不同。

手动配置支持 stdio 的 MCP 客户端时，使用仓库虚拟环境中的 Python，参数为：

```text
-m stardew_ai_runtime.mcp_server --run-dir <Mod 安装目录>
```

Python 可执行文件通常位于 `runtime/.venv/Scripts/python.exe`。请使用绝对路径，工作目录设为仓库根目录。

## 游戏内聊天

复制 `config/chat-backend.example.json` 为 `config/chat-backend.json`，设置 `backend` 和 `model`。当前聊天后端支持 `kimi` 和 `agy`；模型标识应与已配置的客户端一致。

```powershell
uv run --project runtime python -m stardew_ai_runtime.chat_bridge --run-dir "C:\你的游戏目录\Mods\StardewAI.Companion.Mod" --backend kimi
```

保持服务运行，在游戏中按 `F8` 打开对话。自由模式、界面缩放和异常恢复仍在开发中。

## 常见问题

- **找不到游戏连接**：确认已通过 SMAPI 进入存档，`--run-dir` 指向实际加载的 Mod 目录。
- **模型没有游戏工具**：检查当前客户端是否加载了本项目的 MCP 配置，以及 Python 路径是否正确。
- **有回复但没有执行动作**：查看任务结果和错误原因；文本回复不等于动作完成。也可能是背包、材料、位置或版本校验不满足条件。
- **用量显示未知**：所选后端未提供可归属的统计信息，不能视为零消耗。

不要公开账号凭据、`transport-discovery.json` 中的会话令牌或未处理的完整聊天记录。
