# 后端验证状态

以下按已保存证据区分验证范围；旧版本结果不代表当前版本的所有玩法通过。更新日期：2026-10-30。

| 后端 | 已记录的验证 | 范围与限制 |
| --- | --- | --- |
| MiniMax Code（mcode） | 0.3.22 的内置路径、MiniMax Code 0.6.2 原会话接入；三组隔离实机、记忆读档、原生能力学习与升级 | 见[双路径验证](dual-connection-validation.md)；首版原客户端窗口需保持闲置，不代表其他客户端或任意模型通过 |
| Codex CLI | 10 月初 gpt-6.1-sol/high 九场景 9/9 及隔离经营验收 | 见[历史评测](archive/agent-review-results-20261001.md)；当前原会话自动适配范围仍以[接入指南](agent-self-integration.md)为准 |
| agy | 0.3.11 的真实行动、汇报与退出验收 | 见[行为记录](companion-harness-acceptance-20261003.md)及[退出记录](companion-shutdown-acceptance-20261003.md)，不是 0.3.22 全面复验 |
| dsh | 10 月 2 日 deepseek-flash/low 对话与工具交互实测 | 经营决策和当前版本完整实机未据此推定通过 |
| Kimi CLI | 接入、统一提示、用量与会话相关自动化回归 | 此索引尚无当前版本完整实机验收结论 |
| Claude Code 及其他 | 提供通用手动 MCP 接入方法 | 未列为已完成行为验收 |

贡献验证时记录版本、模型、日期、隔离存档、原生回执与失败边界。默认运行[固定三组回归](repeatable-companion-acceptance.md)；读档、跨日、执行中退出和原伙伴接入另验，不用测试跳过代替通过。

旧九场景口径、交互耗时和单次成本推算保留在[10 月 3 日历史基线](archive/tested-backends-20261003.md)。历史使用量与报价不作为当前费用承诺；以实际后端账单和游戏用量记录为准。
