# 0.3.8 修复与验收记录

本轮继续 PR #9（`codex/companion-036-ui-and-runtime`），以原生 F8 对话、伙伴资源恢复、动作动画、运行时唤醒和会话复用为范围。早先 DSH 与 tracked task 回归也包含在全量 runtime suite 中。没有新增授权 LLM、proposal-id 审批门或中文关键词授权规则。

## 问题、修改和文件

| 问题 | 本轮修改 | 主要文件（仓库相对路径） |
| --- | --- | --- |
| DSH 的旧认证诊断污染新进程（PR 中已完成，本轮复验） | 每次真正启动新进程创建独立诊断 context；旧 stderr reader 只修改自己的 context，新进程普通异常回退为 `DSH_RUNTIME_FAILED` | `runtime/src/stardew_ai_runtime/agent_backends.py`, `runtime/tests/test_dsh_protocol.py` |
| 持续接收消息时 completed task 留在集合（PR 中已完成，本轮复验） | 全部 tracked task 注册完成后 discard；断线仍取消并等待未完成任务 | `runtime/src/stardew_ai_runtime/chat_bridge.py`, `runtime/tests/test_chat_bridge.py` |
| milestone 旧 plan-mode 文案（PR 中已完成，本轮复验） | 清理错误信息、注释与测试名；chat/plan 都属于 life conversation，进入某模式本身不代表 adopt 获得授权 | `runtime/src/stardew_ai_runtime/mcp_server.py`, `runtime/tests/test_mcp_server.py` |
| F8 入口笨重，直接回复需绕行 | 未读原生 NPC 对话 → 四项原生选择；回复立即开输入框，历史居中；保留草稿与待决定事项；子页返回选择，根选择 Esc/F8 关闭 | `src/StardewAI.Companion.Mod/Menus/CompanionDialogueController.cs`, `CompanionF8Flow.cs`, `CompanionNpcDialogueBox.cs`, `CompanionSpeechInputMenu.cs`, `CompanionConversationMenu.cs`, `CompanionDashboardMenu.cs`, `LifeMenuUiState.cs`, `ModEntry.cs` |
| 低体力仍继续派活，没有午休恢复 | 低体力收尾后去真实床位，调用原生床上资源恢复；满体力醒来后重新唤醒；夜间睡眠与午休分开保存 | `Execution/CompanionRestController.cs`, `Execution/CompanionMechanicsCoordinator.cs`, `Domain/CompanionBedtime.cs`, `Domain/CompanionActorState.cs`, `Domain/FarmerMechanicsActor.cs`, `Adapters/NormalNativeActionAdapter.Consumption.cs`, `Menus/CompanionTaskPanelState.cs`, `Observation/GameWorldObserver.Production.cs` |
| 空水壶找不到水、补水成功仍报告部分失败 | 近处无水时专项查整张地图，靠伙伴位置选择真实水格；第一次实际补满就停止备选目标 | `runtime/src/stardew_ai_runtime/scheduler.py`, `mcp_server.py`, `src/StardewAI.Companion.Mod/Execution/NativeActionStateMachine.cs` |
| 行动没有动画，浇水末帧卡住 | 行走按实际位移驱动原生帧；单次工具、进食更新原生 Sprite；末帧释放工具动作；效果只提交一次；睡眠闭眼、醒来解除姿态 | `Domain/FarmerMechanicsActor.cs`, `Domain/NativeCompanionAnimation.cs`, `Adapters/NormalNativeActionAdapter.Consumption.cs`, `Execution/WaterZoneStateMachine.cs` |
| 开局等待玩家，模型繁忙时自主唤醒被吞 | 单个待处理唤醒等待模型锁，取最新快照；指纹取得锁后消费；休息期间不给新任务，醒后再次触发；断线未选作业可重试，已选作业保持原任务 | `runtime/src/stardew_ai_runtime/chat_bridge.py`, `autonomy.py` |
| 午休醒来仍在农舍，误判整个农场没有农活 | `farmWork` 在已有快照刷新时扫描真实 Farm；伙伴位置与本地工具观察仍各保留原地图。Python 保留地图与观测状态，未观测数量为未知；自动农活检查实际目标地图，避免派错坐标或虚假 `NO_WORK` | `Execution/CompanionMechanicsCoordinator.cs`, `runtime/src/stardew_ai_runtime/scheduler.py`, `mcp_server.py` |
| 任务尚未执行便显示模型声称的结果 | 选中作业显示待执行，成功或失败由原生终态记录；取消等模型线程确实退出再释放锁 | `runtime/src/stardew_ai_runtime/chat_bridge.py` |
| 持续出现额外 session | 关怀复用持久生活 CID；普通完成事件不改变生活指令指纹；重复保存相同设置不递增 revision；次数不可测量时保持未知 | `runtime/src/stardew_ai_runtime/life_chat.py`, `chat_bridge.py`, `companion_profile.py`, `agent_backends.py` |
| 已确定的同步热点 | 历史布局缓存、作息未变不反复捕获/保存 actor、返程预估缓存并延后到睡前四小时 | `Menus/CompanionDashboardRefreshState.cs`, `CompanionDashboardMenu.cs`, `ModEntry.cs`, `Execution/CompanionRestController.cs` |
| 工具协议与实际能力不一致 | 接受无歧义数组包装及整数坐标别名；非法值在选作业前拒绝；直接暴露进食与实际单批种植；指引与真实工具保持一致 | `runtime/src/stardew_ai_runtime/mcp_server.py`, `docs/agent-core-instructions-draft.md`, `docs/agent-guidance/daily-rhythm.md` |

表内未标根目录的 C# 文件分别位于 `src/StardewAI.Companion.Mod/` 的相应子目录。另更新 `manifest.json`、`StardewAI.Companion.Mod.csproj` 至 0.3.8，增加 `Properties/AssemblyInfo.cs` 的测试访问声明与 `docs/release-notes-0.3.8.md`。

## 原现场 trace 与会话口径

2026-10-03 的 mcode 导出记录当天新建五段会话：两段晨间关怀各 1 次模型请求；工作会话分别是 9 轮桥接决策 / 57 次模型请求、2 轮 / 15 次；生活会话 1 轮 / 5 次。这五段合计 79 次，不能当作全天总数。另一个命令审计文件的 17 条命令 / 五个 CID 包含两个续用旧 CID，却没有记录那两个关怀 CID；两种口径合起来触达七个 CID，并非当天新建七个。

确定异常是关怀每次传空 CID 并丢掉返回 CID。旧生活指纹使用普通 `memoryRevision`，重复 profile 保存也递增 revision，造成不必要轮换。现场日志明确有 12:02:09、12:13:53 的工作 profile 轮换和 12:07:31 的生活轮换，但无法仅凭日志认定每次 profile 变化都来自重复保存。

修后每后端、每存档维持工作和生活两条持久会话，关怀并入生活，两条共用模型锁。真实设置/约定变更、上下文预算、无法测量时的桥接次数 checkpoint、agy 日结仍可正常轮换；升级首次采用新指纹时也可能轮换。不会承诺永远只有两个 CID。旧 trace 中将 exec 计作一次的历史记录不倒改。

## 回归测试

- 全量 Python runtime：1094 通过，0 失败、0 错误、0 跳过，68.53 秒。
- 全量 Mod：660 通过，0 失败、0 跳过。
- 全量 Transport：37 通过，0 失败、0 跳过。
- Release 构建：0 警告、0 错误；修改的 Python lint、边界扫描与 diff 检查通过。
- `test_runtime_dispatch_regressions.py`：模型忙时保留最新唤醒、并发关怀复用 CID、同设置不轮换、实际终态反馈、取消等待线程退出、反复取消以及断线重试/已选作业去重。
- `test_recovery_tool_contract.py`：包装数组、坐标别名和无效坐标、实际种植/进食工具、全地图补水 fallback 与作业选择边界；新增 7 项地图范围回归，验证农舍占位零/不可观测地图为未知，农舍中仍可查 Farm 的真实 5 株作物，错误范围不派发自动浇水/收获。
- 游戏侧新增 `CompanionF8FlowTests`, `CompanionDashboardRefreshStateTests`, `CompanionRootEscapeTests`, `BedtimeProjectionTests`, `CompanionRestRecoveryTests`, `NativeCompanionAnimationTests`；扩充 `CompanionBedtimeTests`, `NativeActionStateMachineTests`, `WaterZoneStateMachineTests`，覆盖草稿/未读、菜单归属与事件、缓存、午休/缺床/暂停/夜间、动画推进、补水备选目标和已生效动作核对。
- `SnapshotSchemaTests` 验证伙伴在 FarmHouse 时仍扫描真实 Farm 的 5 格待浇作物，并保持角色与本地工具的地图范围。
- 既有 DSH restart 回归覆盖上一进程认证诊断及旧 reader 迟到；chat bridge 持续消息回归验证 completed tracked tasks 不靠 receive timeout 回收；milestone 语义测试使用 chat/plan 同属 life conversation 的实际规则。

完整变更列表以 PR diff 为准；实机辅助脚本、原始 session 内容、模型配置和本机日志仅留在本地测试证据中。

## 实机与包

最终便携包源码为 `895ce83907b8454c536541739fb65c06f7526bb0`，`sourceDirty=false`，内置 Python 3.13.11。

- 包名：`StardewAI.Companion.Mod-0.3.8-windows-x64.zip`
- ZIP SHA-256：`016515aff10a0c891bf5e517f63594588fd2a14fb24fc38227a22857b94b2195`
- Mod DLL SHA-256：`6c1d659377ac5e45142ac989b1441fb779cd12c5bbb579db4b20fffb1f2fe7f1`
- 1772 个包文件受清单管理；源码、运行时依赖与原生 DLL 成套验证。

实机平台为 Stardew Valley 1.6.15 / SMAPI 4.2.1，后端 agy，模型 `gemini-3.8-flash-low`。最终验收 `agy-recovery-038-r12` 使用独立测试存档、Mods 与 APPDATA，载入的 DLL 哈希与上述最终包一致。日常 Mods、日常存档与 Golden 源的前后哈希一致，临时 agy MCP 配置已恢复，测试游戏已正常退出。

十项专项实机断言全部通过：

| 验收项目 | 实际证据 |
| --- | --- |
| 开局自主补水和浇水 | 启用已有闲时自主偏好后，不发送行动指令；空壶 0 → 原生补满 40 → 浇水后 39，1 株干作物变为已浇水 |
| 行走和工具动画 | 行走采到多个原生帧；浇水采到 46、62、63，后续续工采到 45、46、58、59、62、63，原生动作有实际推进 |
| F8 四项选择与草稿 | 四个子入口均打开对应页面；直接回复输入框立即出现，返回再进入保留未发送草稿；实际消息发送给 agy |
| F8 关闭 | 返回根选择后 Esc 关闭，玩家可移动且游戏时间可继续 |
| 真实床恢复 | 注入伙伴体力 1、无食物；7:50 已到 FarmHouse 的真实床位，体力 9；8:00 体力 16 |
| 菜单暂停 | 打开游戏背包，两个间隔快照的游戏时间与伙伴体力均不变 |
| 满体力醒来 | 11:10 体力 270，当天醒来，原生 `isInBed=false`，入睡时间与午休标记清除 |
| 醒后自主续工 | 无玩家行动指令，自动导航回 Farm 并浇完 5 株作物；13:00 体力 260、水量 35、待浇作物 0 |
| 未读原生 NPC 对话 | 重新打开 F8 先显示真实 agy 回复；可见绘制后未读数为 0，读完进入四项选择，最终正常关闭 |

本轮还实际遇到一次模型在农舍直接选择农场浇水：地图范围检查拒绝派发，记录 `STEP_DISPATCH_REJECTED`；模型取得反馈后改为先导航再浇水并成功。因此十项通过不等于每次模型选作业都一次成功。

正常未暂停游戏期间采集 18,644 个帧间隔：中位数 16.6663 ms、P95 16.6956 ms、P99 17.0157 ms、最大 80.5333 ms；超过 100 ms 与 250 ms 均为 0。此口径排除加载、菜单与主动截图，不能据此保证所有保存、加载或驱动卡顿已消失。

本机证据目录为 `.test-runs/agy-recovery-038-r12/evidence/`，专项结果见 `agy-recovery-acceptance.json`，外层运行结果与帧统计见 `scenario-evidence.json`。外层功能通过字段为真；其通用“背包完全不变”字段为假，原因是清理杂草期间玩家背包由无纤维变为 2 个纤维。该字段还被合并进通用 `playerResourcesInvariant`，因此后者也为假，不能表述为通用资源不变检查通过。专项原生快照中的玩家体力始终为 270、金币为 500；床恢复没有借用玩家体力。旧 mcode 的会话修复有回归覆盖，本轮修后实机后端是 agy，没有将其作为 mcode 实机复测。

## 行为兼容性与边界

旧存档无需迁移；没有午休标记的存档按夜间睡眠解释。F8 入口和白天低体力行为按本轮需求变化；自主开局与醒后续工仍要求启用已有的闲时自主偏好。chat/plan 均为生活会话的现有语义保留，未恢复严格 plan-mode gate。工具效果、玩家物品/体力和暂停/取消记录继续以原生观察为准。

`farmWork` 明确代表 Farm 的观测；角色在其他地图时也能看到实际 Farm 工作。本地工具观察继续保留自身范围；未观察结果为未知而非零。自动农活在地图不匹配时明确拒绝，要求模型先导航，不会自动偷偷派发另一个任务。

按用户纠正，本轮不实现额外授权 guard。当前系统仍由现有模型根据玩家对话、持久偏好与约定判断授权；工具参数验证、单模型串行、原生短作业及终态反馈约束执行过程，并不构成独立中文授权分类器。进食动画有离线原生语义和回归验证，本轮无食物的实机用例没有验证进食；行走、浇水与床上姿态有实际原生帧证据。普通导航/NativeAction 的所有事件暂停组合不在此次补丁的全面验证范围内。
