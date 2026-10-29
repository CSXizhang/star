# 双来源续玩验收

这些辅助只操作已运行的隔离 `free-mode-control` 游戏。先用 `tools/launch-game.ps1` 启动已校验发行包，指定独立 RunId、`-PresetAcceptance`、`-NoOrchestrator` 及 `-ExpectedProdDllSha256`；不要使用玩家日常存档目录。一次只运行一个控制脚本。

`continuity_probe.py <隔离运行目录> <动作> [参数]` 通过真实 Mod 菜单、聊天和原生游戏动作取证，不直接填写工作成果。已实机验证的流程：

1. `source builtin` 核验真实来源按钮、确认回执及配置落盘；`forestry <证据标签>`、`logistics <证据标签>` 分别复用固定林业和完整物流预设。预设会重新布置隔离夹具，只能放在新任务起点，续档与跨日检查期间不要重布置。
2. `send <自然语言约定>`、`sleep`、`snapshot` 核验第二日；`sleep` 等原生寻路走进床后点击实际睡觉选项，使用原生出货确认，日期由游戏推进。
3. `save-exit` 先取消执行，调用原生保存并退出；等待唯一游戏进程结束，用 `restart-continuity.ps1 -RunDir <相同目录> -GamePath <游戏目录>` 重启。它保留原配置、绑定与存档，只恢复观察坐标，不重置作物、物品或体力。再次 `identity`、`snapshot`，继续睡到第三日并核对约定和库存。
4. `source external` 的证据同时比较切换前后的箱子、背包和作物。由原客户端明确连接原会话后再发游戏消息；`identity` 记录原会话 ID，不导出令牌或会话历史。`harvest <证据标签>` 验证收获入箱，再做两次真实跨日及一次完整退出续档。新增记忆只从游戏聊天注入，回原客户端时用不含答案、无游戏工具的问句核对。
5. 完成伙伴旅程并正常退出后，用相同重启脚本加 `-Mode player` 验证 AI 自玩：`f9` 明确交权，`start <自然步行要求>` 提交任务，`takeover-active` 等原生动作执行后经生产 F9 入口接管。`verify-human-hold` 连续采样位置和控制权；来源来回切换后再次执行，确认旧导航没有恢复。

所有原生样本、来源确认、回合及夹具报告保存在隔离目录 `evidence`。`source` 的成功表示来源确认与保存完成；外部任务开始前仍须确认 `connectionStatus=connected`。历史消息的 Speaker 不改写，新外部消息使用客户端明确提供的别名，回切内置伙伴应恢复存档名字。

外部 ACP 若不产生内置后端的日志，可由原客户端提供**仅该隔离会话的公开工具回执**，使用 `verify_external_receipts.py <原生报告.json> <公开回执.json>` 补验同一真实回合的动作终态。回执数组须含 `sessionId`、`timestampMs`、`toolCall`；工具记录包含 `name/status/input/output`。补验按原会话 ID 和实际回合时间筛选，保留原报告，不伪造日志或重做已经完成的动作。

本机旧截屏读回入口曾额外纵向翻转画面；保留原始截图，不把倒置的证据图当作游戏界面缺陷。动作是否成功以原生状态、终态回执及独立的移动/动画样本共同判断。
