# 星露谷伙伴领域指导索引

涉及经营选择时，读取对应主题；已明确的单项动作沿用执行 skill。各主题只维护自己的判断方法，共享的派工、失败处理与接续规则见[核心指令](agent-core-instructions-draft.md)。

等待营业、生长等条件是正常经营状态：通过 `manage_todo` 建立 `gameTime`（year、season、day、timeOfDay，HHMM）或 `calendar` 触发条件，记录条件满足后要做的事，然后结束本轮决策。游戏时钟自然运行，待办到期自动唤醒；不需要空导航、反复观察或重复 `NO_WORK` 消耗轮次，也不因商店此刻未开就认定整天不能采购或销售。条件改变后，先对相同目的地 `query_route`，新 `reachable=true` 证据可解除旧导航封锁。具体时间和采购数量依据原生营业与资源事实选择。

| 主题 | 需要解决的问题 |
| --- | --- |
| [种植选择](agent-guidance/crop-selection.md) | 选什么作物、买多少、收益与照料规模如何匹配 |
| [田区规划](agent-guidance/farm-region.md) | 选哪片地、怎样连续扩张、清障与绕路怎样取舍 |
| [养殖与加工](agent-guidance/livestock-processing.md) | 如何启动生产、安排照料、收取入箱与续投 |
| [采购与行程](agent-guidance/procurement-travel.md) | 这趟是否来得及、要带什么、怎样合并行程 |
| [知识查询](agent-guidance/wiki-lookup.md) | 补齐哪些字段、读取哪些资料、怎样复用知识 |
| [日内节奏](agent-guidance/daily-rhythm.md) | 天气与节日怎样影响当天安排、何时收尾与过夜 |
