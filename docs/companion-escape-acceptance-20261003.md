# 0.3.9 Esc 关闭修复与验收

Esc 的生产输入入口此前只处理 F8 根选择。未读 NPC 对话及回复输入框依赖游戏自身转发按键，且关闭分页会继续调用“下一条/返回选择”回调，不能可靠结束整段对话。

本版在 `src/StardewAI.Companion.Mod/ModEntry.cs` 将 Esc 转入 `Menus/CompanionDialogueController.cs` 的统一处理：只接管当前自己拥有的伙伴对话，清除分页与后续回调，关闭窗口并恢复移动、语言和输入焦点。输入框仍通过原有退出清理保存草稿，不调用发送或返回选单回调。其他游戏菜单、事件与历史/设置子页的现有返回行为保留。

增加 `src/StardewAI.Companion.Mod.Tests/CompanionRootEscapeTests.cs` 的 3 个回归用例，验证 F8/直接互动的对话输入、菜单归属与事件排除；更正一个旧测试名。另将 Mod 项目及 manifest 升为 0.3.9，增加 `docs/release-notes-0.3.9.md`。没有修改 Python runtime。

## 验证

- 全量 Mod：663 通过，0 失败、0 跳过。
- Release 构建：0 警告、0 错误；diff 检查通过。
- Stardew Valley 1.6.15 / SMAPI 4.2.1，agy + `gemini-3.8-flash-low`，最终独立实机轮 `agy-escape-039-r2` 的 7 项专项断言全部通过：根选择关闭、输入框关闭后不重开、草稿保留、草稿未发送、真实 agy 未读 NPC 对话关闭后不重开、移动/时钟恢复、历史 Esc 仍返回选择。
- 实机辅助工具在游戏线程调用生产 Esc 处理入口及原生菜单，随后等待再读取真实游戏状态；不是仅断言纯函数。窗口关闭后 `_next`、`_pages`、`_afterPages` 均为空，玩家可移动、游戏时间可继续、原生 `dialogueUp=false`。
- 本轮专项验证 Esc；没有将 0.3.8 的恢复/补水整组实机用例算作本轮再次运行。
- 日常 Mods、日常存档和 Golden 源前后哈希一致；临时 agy MCP 配置已恢复；游戏正常退出。本轮玩家资源与背包不变检查均通过。

首轮辅助脚本误将晨间关怀创建的生活 CID 当成“草稿已发送”，因而中止。改为检查正式对话记录中没有草稿后，最终轮全部通过；未因此修改产品的会话行为。

本机证据见 `.test-runs/agy-escape-039-r2/evidence/escape-039-acceptance.json` 与 `scenario-evidence.json`；测试结果见 `.local/escape-039-results/escape-039.trx`。

## 包与兼容性

- 便携包：`StardewAI.Companion.Mod-0.3.9-windows-x64.zip`。
- 源码：`2ef4f3f1a08e2c571dbb5cf7e24c0ed032b94bf8`，`sourceDirty=false`；后续仅补验收文档。
- ZIP SHA-256：`578e0a3192b1951dbcf9d38353c00f23dfa2df58ac0ce44459774cd40fc467cf`。
- DLL SHA-256：`221aa36bbbab57d9816ae36bbe428167141f8f4fc0ffe4e8b32e32ffd178143a`；实机载入的 DLL 与包一致。

旧存档无需迁移。唯一预期交互变化是 NPC 对话和回复输入中 Esc 直接结束整段对话；点击输入框“返回”仍回到选择。沿用 0.3.8 的动画、恢复、补水和自主性修复，没有新增授权 guard。
