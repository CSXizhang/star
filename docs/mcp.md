# MCP 与聊天接入

本项目通过本地 MCP 服务向模型客户端提供游戏观察和动作工具。游戏内聊天另由 Python 聊天服务转发。模型客户端需要自行安装和登录。

## 接入前

先按[首页](../README.md)编译和安装 Mod，通过 SMAPI 启动游戏并进入存档。Mod 会在自己的 `data` 目录下生成 `transport-discovery.json`，供 Runtime 发现本地连接。

当前动作执行依赖 Mod DLL 与本地配对清单一致：干净克隆可从源码编译后运行 `tools/setup-companion.ps1` 生成 `local-dev` 绑定完成安装；缺少清单时返回 `COMPATIBILITY_UNKNOWN`，DLL 与清单不一致时返回 `MOD_RUNTIME_MISMATCH`（重新运行设置向导同步）。

## 注册 MCP

在仓库根目录运行，将路径替换为实际 Mod 安装目录：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/register-mcp.ps1 -RunDir "C:\你的游戏目录\Mods\StardewAI.Companion.Mod" -Agent kimi -Install
```

首次使用 Kimi 时，在仓库根目录交互运行 `kimi`，核对项目信任提示中的 MCP 启动命令和 Mod 路径后确认信任。输入 `/mcp`，确认 `stardew-companion` 已连接，再启动下文的聊天服务；已打开的 Kimi 会话需重新启动才能加载新注册的工具。

`-Agent` 支持 `kimi`、`agy`、`claude`、`codex`、`dsh` 和 `all`。省略 `-Install` 时输出配置供你检查；不同客户端的注册方式和可用程度可能不同。

手动配置支持 stdio 的 MCP 客户端时，可以直接使用 `uv run --project <仓库根目录>/runtime python`（与 `register-mcp.ps1 -Install` 生成的配置一致），参数为：

```text
-m stardew_ai_runtime.mcp_server --run-dir <Mod 安装目录>
```

也可以使用仓库虚拟环境中的 Python（通常位于 `runtime/.venv/Scripts/python.exe`），效果相同。请使用绝对路径，工作目录设为仓库根目录。

## 游戏内聊天

复制 `config/chat-backend.example.json` 为 `config/chat-backend.json`，设置 `backend` 和 `model`。当前聊天后端支持 `kimi` 和 `agy`；模型标识应与已配置的客户端一致。

```powershell
uv run --project runtime python -m stardew_ai_runtime.chat_bridge --run-dir "C:\你的游戏目录\Mods\StardewAI.Companion.Mod" --backend kimi
```

保持服务运行，在游戏中按 `F8` 打开对话。发送指令后窗口会关闭，让游戏动作继续；再次按 `F8` 可查看同一条指令的后续进度和完整回复。打开菜单会暂停农活动作的推进，关闭菜单后才会继续执行。

- **暂停 / 继续**：暂停保留当前指令，继续后从已有进度推进。按钮显示“请求中”时请等待服务确认；确认前不会显示为已成功。
- **取消后改做别的事**：点击取消，等取消确认后再发送新指令。尚未发送的文字会保留，关闭并重开 F8 后也可继续编辑。
- **查看长回复**：在聊天记录内滚动阅读。查看旧消息时新回复不会强制把视图拉到底部，可通过新消息提示回到最新内容。
- **连接异常**：界面会保留未确认状态并提示重试。游戏中的动作可能已收到本地暂停或取消请求，服务未确认不代表它们已恢复；恢复连接后再确认控制结果。

## 常见问题

- **找不到游戏连接**：确认已通过 SMAPI 进入存档，`--run-dir` 指向实际加载的 Mod 目录。
- **模型没有游戏工具**：检查当前客户端是否加载了本项目的 MCP 配置，以及 Python 路径是否正确。
- **有回复但没有执行动作**：查看任务结果和错误原因；文本回复不等于动作完成。也可能是背包、材料、位置或版本校验不满足条件。
- **用量显示未知**：所选后端未提供可归属的统计信息，不能视为零消耗。

不要公开账号凭据、`transport-discovery.json` 中的会话令牌或未处理的完整聊天记录。
