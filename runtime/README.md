# 星露谷物语Stardew Valley Agent · Runtime

Python Runtime 提供 MCP 工具、游戏内聊天服务、计划执行、按存档保存的工作记忆和用量统计。通过本地通信连接 SMAPI Mod，游戏动作由 Mod 执行。

完整安装和使用说明见[仓库首页](../README.md)。在仓库根目录运行：

```powershell
uv sync --project runtime --locked
uv run --project runtime pytest runtime/tests
uv run --project runtime ruff check runtime
```

启动聊天服务时，`--run-dir` 指向已安装的 Mod 目录。模型后端在本地 `config/chat-backend.json` 中配置，可从 `config/chat-backend.example.json` 复制。游戏需已进入存档，所选模型客户端需先完成登录和 MCP 注册。
