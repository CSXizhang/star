# Windows 发行包安装

发行包提供编译好的 Mod、伙伴程序和独立 Python 环境。玩家无需克隆源码或安装开发工具；游戏、SMAPI 和模型客户端由你自己准备。

## 下载与安装

1. 在 [GitHub Releases](https://github.com/CSXizhang/star/releases/latest) 下载 `StardewAI.Companion.Mod-0.3.7-windows-x64.zip`。GitHub 自动提供的 Source code 是开发源码，不是玩家安装包。
2. 解压，打开其中的 `StardewAI.Companion.Mod` 文件夹。关闭游戏，双击 **设置星露谷伙伴.cmd**，选择游戏目录并按提示安装。
3. 向导把整包安装到 `游戏目录\Mods\StardewAI.Companion.Mod`，然后让你选择 Kimi、agy、Codex（GPT）、dsh（DeepSeek）或 MiniMax Code（mcode），以及使用的模型。已有配置和 `data` 会保留。

也可以自行把整个文件夹复制进游戏的 `Mods`，然后运行该文件夹中的设置入口。不要只复制 DLL：`runtime`、`tools`、`release-manifest.json` 等也属于运行所需文件。

当前包面向 Windows x64、Stardew Valley 1.6.15 和 SMAPI 4.2.1。SMAPI 请按其安装说明单独安装，发行包不会替你安装游戏或 SMAPI。

## 模型登录与工具信任

在向导中选择你已有账号的模型后端，并按该客户端自己的说明安装、登录。向导发现 CLI 缺失时会提示，不会自动购买账号或安装模型客户端。

- **Kimi CLI**：设置会把伙伴 MCP 配置写到已安装 Mod 内的 `.kimi-code/mcp.json`。第一次使用，在该 Mod 文件夹打开终端运行 `kimi`，确认该文件夹及伙伴工具的信任提示；可用 `/mcp` 查看工具连接。完成后退出检查会话，再开始游戏内聊天。
- **agy**：设置时选择 agy 会注册名为 `stardew-companion` 的 MCP 工具。agy 的这项注册是客户端级配置；如果之前有同名配置，先留意它指向的目录。按 agy 自己的流程完成登录，并选择账号支持的模型。
- **Codex（GPT）**：先在本机安装 Codex CLI 并完成登录。向导中的模型可以留空，沿用本机 Codex 配置；伙伴工具只绑定到当前游戏运行，不需要修改全局 MCP 配置。
- **dsh（DeepSeek）**：先安装并登录本机 dsh，再在向导中选择 `dsh`，模型填 `deepseek-flash`（V4.1 Flash），默认 low 推理。伙伴使用 dsh 的会话协议与临时游戏工具；复用本机已保存的 DeepSeek 凭据，也支持 `DEEPSEEK_API_KEY`。向导不读取展示、复制或写入你的账号密钥。切换后端前先退出游戏与伙伴服务，再重新启动。
- **MiniMax Code（mcode）**：先安装 MiniMax Code CLI（Windows 可用官方安装器，或 `npm install -g @minimax-ai/code`），并完成 `mcode login`；本机已登录 MiniMax Code 桌面端时可直接复用。向导中选择 `mcode`，模型可留空沿用本机默认模型，默认 low 推理。伙伴每轮用一次 `mcode exec`，只把那一次的游戏工具写进本次游戏运行目录，不修改你的全局 MCP 配置。切换后端前先退出游戏与伙伴服务，再重新启动。绑定细节见[接入文档](mcp.md#minimax-code-的工具绑定方式)。
- **外部 Codex 对话**：安装后也可将工具和伙伴技能注册到自己的 Codex 项目，直接沿用外部对话接入游戏，见 [Codex 直接接入](mcp.md#codex-直接接入)。同一时间选一个入口派工，保留自动启动的伙伴服务，不重复启动。
- **外部 MiniMax Code 对话**：MiniMax Code 的 MCP 服务保存在数据目录的 `mcp.json`（默认 `%USERPROFILE%\.minimax\mcp.json`），桌面端与 CLI 共用。用 `tools\register-mcp.ps1 -Agent mcode -Install` 可合并写入该文件（保留其他服务），或在 MiniMax Code 里用内置 MCP 工具注册同名 stdio 服务。游戏内后端每轮自己绑定工具，两者互不干扰。

工具读取当前存档需要游戏已通过 SMAPI 启动并进入存档。遇到尚未连接游戏的提示，可以先完成登录，再进游戏检查。模型名称应使用客户端实际支持的标识。

## 开始相处

通过 SMAPI 启动游戏并进入存档，Mod 会在后台启动自己目录内的伙伴服务。等待连接后，走近伙伴按交互键即可开始原生头像对话；初次见面可以给伙伴起名、选择简单的相处方式。F8 直接打开伙伴对话，查看聊天记录和待决定事项，并输入回复；计划、用量和具体设置放在“更多”中。主动帮忙只决定空闲时是否自行找事，关闭后仍会执行你交代的工作。伙伴默认午夜前收尾上床，可在作息设置或对话中调整，不替玩家结束当天。

详见[玩家指南](companion-guide.md)。设置、账号或工具连接需要调整时，再运行已安装 Mod 目录内的 **设置星露谷伙伴.cmd**。

## 升级与排查

升级前关闭游戏和伙伴服务，备份旧 Mod 的 `data` 及 `config`，再用新发行包的设置向导安装到相同游戏目录。保留这些目录可继续使用原有伙伴设置、记忆和模型配置；不要把不同版本的 DLL 与运行文件随意拼在一起。

- **没有连接**：检查模型 CLI 是否已安装登录、设置选择的模型是否可用，以及 SMAPI 是否成功加载伙伴。重新运行已安装目录内的设置入口检查配置。
- **需要查看服务启动过程**：可手动运行已安装目录内的 **启动伙伴服务.cmd**。它与游戏内自动启动使用相同的随包程序和配置。
- **路径或文件不完整**：重新解压完整发行包并运行设置，不要从源码 ZIP 中寻找可运行 DLL。

源码开发和其他 MCP 客户端的手动配置见[接入文档](mcp.md)与[参与开发](../CONTRIBUTING.md)。
