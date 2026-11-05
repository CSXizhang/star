# 提示词来源与维护

`agent_instructions.py` 加载核心指令与领域索引，`consult` 按需读取主题指导；外部伙伴使用 `agent-skills/stardew-companion`。这些都是运行输入，文件名中的 `draft` 不表示可以删除。

- [核心指令](agent-core-instructions-draft.md)、[领域索引](agent-domain-guidance-draft.md)与 [执行方法](../agent-skills/stardew-companion/references/ingame-execution.md)共同维护当前游戏规则。
- 聊天与工作使用同一套八个工具；动作立即执行并返回结果。忙碌、暂停和接管在执行时校验，不新增持久化动作队列。
- 按来源恢复原会话；提示词变化不授权静默更换会话。送达基线只在确认输入送达后推进，读档和主动压缩后重建。
- 各后端通过共用游戏提示限定信息来源，不额外关闭客户端工具。客户端能力不等于游戏内允许使用的能力。

改动先验证指令加载、工具契约、会话延续和增量送达，再按影响运行[实机回归](repeatable-companion-acceptance.md)及读档、退出专项。发行包复制同一份运行输入，不能只更新仓库文档。

2026 年 10 月初的旧队列、会话轮换和客户端配置方案见[历史接入与验收](archive/agent-prompt-integration-20261001.md)、[修改简报](archive/agent-prompt-revision-brief-20261001.md)、[评测结果](archive/agent-review-results-20261001.md)；不再作为当前实现要求。
