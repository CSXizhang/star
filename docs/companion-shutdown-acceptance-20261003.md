# 0.3.10 退出释放验收 — 2026-10-03

游戏退出后，随包 Python 服务继续运行并占用 `_asyncio.pyd`，导致安装更新无法覆盖文件。修复让自动后台与启动它的游戏绑定，并清理其创建的子进程。

## 现场与修复

现场日常 Mod 仍是 0.3.7。Python 7444 从该 Mod 的 `runtime/python/python.exe` 启动，父启动器为 PowerShell 8596；原游戏 27348 已不存在。核对安装路径、父子关系和模块占用后，只结束这条确认已失去游戏的旧后台；启动器随后退出。等待系统释放模块后，`_asyncio.pyd` 独占读写打开成功，文件哈希仍与原发行清单相符。

- Mod 保存自己启动的进程句柄。返回标题和正常进程退出时，停止该启动器及子进程；启动与停止同步，覆盖环境检查期间退出的竞态。
- 开发和发行启动入口透传 `OwnerProcessId` / `--owner-pid`。自动后台在创建 ChatBridge 前持有游戏的 Windows 进程句柄，避免只依赖 PID 或 WebSocket 状态。
- Python 将自己加入带 `KILL_ON_JOB_CLOSE` 的独立 Windows Job，后续模型和 MCP 子孙进程随后台退出清理。守护线程独立于事件循环监测游戏退出；先取消正常运行，若线程、模型或解释器退出阻塞，约 5 秒后结束后台，操作系统关闭 Job 并清理其进程。
- 重新进入存档可以重新启动后台。临时断线继续重连；未传 owner 的手动服务仍按原有方式常驻。不会按 Python、PowerShell 或 AI 客户端名称批量清理进程。

日常安装目录没有更新为测试版，日常存档没有改动。升级需要使用新的完整 0.3.10 发行包。

## 修改文件

| 文件 | 修改 |
| --- | --- |
| `src/StardewAI.Companion.Mod/Transport/ReleaseBridgeLauncher.cs` | 保留自动启动句柄、传 owner PID、停止所拥有的进程树 |
| `src/StardewAI.Companion.Mod/ModEntry.cs` | 返回标题和进程退出时停止后台，防止关闭期间晚启动 |
| `tools/start-companion.ps1`、`tools/release-package.ps1` | 开发与发行入口透传 owner 参数 |
| `runtime/src/stardew_ai_runtime/owner_process.py` | 保留游戏身份、Windows Job 和独立退出守护 |
| `runtime/src/stardew_ai_runtime/chat_bridge.py` | CLI owner 参数与后台生命周期绑定 |
| `runtime/tests/test_owner_process.py`、`runtime/tests/test_owner_launcher.py` | 新增 22 项 Python 回归测试 |
| `src/StardewAI.Companion.Mod.Tests/ReleaseBridgeLauncherTests.cs` | 新增 3 项原生启动/退出回归，更新参数断言 |
| `src/StardewAI.Companion.Mod/StardewAI.Companion.Mod.csproj`、`manifest.json` | 版本更新到 0.3.10 |
| `docs/release-notes-0.3.10.md` | 版本说明 |

## 自动回归

```powershell
& runtime/.venv/Scripts/python.exe -X utf8 -m pytest runtime/tests -o addopts='' -q
& runtime/.venv/Scripts/python.exe -m ruff check runtime
$env:STARDEW_GAME_PATH = 'E:\Game\steam\steamapps\common\Stardew Valley'
& .\.local\dotnet\dotnet.exe test src/StardewAI.Companion.Mod.Tests/StardewAI.Companion.Mod.Tests.csproj --no-build --nologo
git diff HEAD^ HEAD --check
```

- Python runtime 全量 **1116 通过**，耗时 74.41 秒；Ruff 和补丁检查通过。使用发行入口同样的 UTF-8 模式。
- Mod 全量 **666 通过，0 失败，0 跳过**，报告耗时 4 秒；相关过滤测试 22/22 通过。
- Release 构建 **0 警告、0 错误**。
- 新测试覆盖正常 owner 结束、事件循环/executor/finally/非 daemon 线程阻塞、强杀后台、孙进程清理、外部进程保护、句柄身份、手动入口、参数透传、临时断线，以及启动检查/立即关闭/重新启动竞态。

## agy 实机退出专项

Stardew Valley 1.6.15 / SMAPI 4.2.1，`agy + gemini-3.8-flash-low`。从 0.3.10 完整发行树另复制隔离测试目录并配置 agy；使用 Mod 的正式自动启动入口，明确关闭 manual bridge 绕过。不由验收脚本自行创建 ChatBridge。测试在生产后台已连接且真实 agy CLI 已启动时操作原生游戏生命周期。

| 隔离运行 | 专项结果 |
| --- | --- |
| `agy-shutdown-0310-normal` | **6/6**：自动启动与握手、真实 agy、返回标题清理并覆盖文件、外部 Python 不受影响、重新入档启动新后台、正常退出清理并覆盖文件 |
| `agy-shutdown-0310-forced` | **4/4**：自动启动与握手、真实 agy、只强制结束准确的游戏进程后后台自行清理并覆盖文件、外部 Python 不受影响 |

每次释放检查要求观察到的启动器及子进程全部消失，且该测试 Mod 路径下没有残留运行进程；随后实际用 `Copy-Item -Force` 覆盖测试目录的 `runtime/python/_asyncio.pyd`，并与发行源文件校验 SHA-256。退出检查上限 15 秒，三次实际覆盖均成功。强制退出轮日志记录了游戏 owner 结束和后台取消。测试结束后没有日常或测试 Mod 的随包 Python 残留。

证据位于两轮 `.test-runs/<运行名>/evidence/lifecycle-acceptance.json`。两轮日常 Mods **42,655 文件**、日常存档 **22 文件**、Golden 源 **4 文件**的前后哈希集合均一致；临时 agy MCP 配置逐字节恢复；实际测试 DLL 与发行清单哈希相符。

本次通过的是退出生命周期专项。通用 `free-mode-control` 功能门未计作通过：正常轮迅速返回标题/重入并退出，没有捕获到它要求的有效渲染截图；强制退出轮主动结束游戏，无法完成通用场景的结尾记录。没有把外层脚本退出码当作通用功能验收成功。本次也未重测 0.3.8/0.3.9 的种植、动画和 UI 交互；相应历史证据见此前验收文档。

## 发行包

- 文件：`artifacts/releases/companion-shutdown-20261003/StardewAI.Companion.Mod-0.3.10-windows-x64.zip`
- 干净源码：`f5d53039ab521b0bc28bc6745882466ba49fda3f`，`sourceDirty=false`；后续只补本验收文档。
- Mod DLL SHA-256：`a33faf5d9fc6c81706d83e4309aabcbfab7febaf02b2bc626b0a93e939898d55`
- ZIP SHA-256：`2ed64b447a45fdded1ebc0b8c0b3196e03612676c38cc0fab4f17a39a3cdef51`
- 发行清单 **1773 个文件**均逐一校验通过；隔离测试配置和测试驱动未写入发行 ZIP。

旧存档无需迁移。行为变化仅为游戏自动后台现在随游戏生命周期结束；手动无 owner 后台保留常驻行为。本轮没有新增授权 guard、意图模型、审批门或生活协议重构。
