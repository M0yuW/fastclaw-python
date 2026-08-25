---
name: match-data-toolkit
description: 受控足球比赛数据工具包；赛事标识来自受审目录，TheSportsDB 确认比赛身份，ESPN 仅补充详情，赔率独立降级。
license: Apache-2.0
env:
  - name: ODDS_API_KEY
    description: The Odds API 密钥，仅由 Runtime 从环境变量注入；未配置时返回结构化 unavailable。
    required: false
---

# 足球比赛数据工具包

所有脚本位于 `scripts/`。事实查询必须先确认赛事、赛季、日期和对阵；不得用模型记忆补造实时事实。

## 数据源策略

- TheSportsDB 是赛程、结果、积分榜和比赛身份的主来源。
- ESPN 只补充详情、场地、近期 form、H2H 和事件。403、超时、空数据、畸形 JSON、结构变化或赛事/球队/日期不一致时，保留主来源结果并显式降级。
- The Odds API 与体彩是独立赔率来源，不得修改主比赛身份。数据分析师只查询比赛数据，赔率分析师只查询 Odds API 或其他市场来源；这两个角色都不得访问体彩/Sporttery。只有 EV 分析师可以调用 `sporttery_data.py` 读取官方 SP，其他角色在 Odds API 不可用时必须返回 `odds unavailable`。
- 每个来源输出 `source`、`status`、`as_of`，失败时只输出脱敏的 `error_code` 和 `safe_reason`。
- 禁止把 URL、ESPN slug、TheSportsDB league ID、Odds API sport key 或 `apiKey` 作为模型参数。

## 命令

- `python3 scripts/espn_data.py --list-competitions`
- `python3 scripts/espn_data.py --competition "瑞典超" --schedule --date 2026-08-10`
- `python3 scripts/espn_data.py --competition "FIFA World Cup" --summary --event 760496`
- `python3 scripts/odds_data.py --competition "瑞典超" --regions eu --markets h2h,totals --odds-format decimal`
- `python3 scripts/odds_data.py --competition "FIFA World Cup" --commence-from 2026-06-01T00:00:00Z --commence-to 2026-08-01T00:00:00Z`
- `python3 scripts/sporttery_data.py --match "IK Sirius" "IF Brommapojkarna"`
- `python3 scripts/ledger_report.py --pending`

`odds_data.py` 先查询实时 sports catalog，再确认受审 `sport_key`。`ODDS_API_KEY` 只从环境变量读取，不进入输出、日志、数据库或异常。`sporttery_data.py` 仅限 EV 分析师使用；其他角色不得以任何方式回退到体彩或猜测市场数据。

`ledger_report.py` 读取 Runtime 注入的 `FOOTBALL_LEDGER`，按固定模板输出统计摘要和 Markdown 表格；它不是账本 JSON 导出器。客户可见结果必须使用该表格格式，不提供 ledger JSON 下载。

旧的 `match_data.py` 世界杯命令继续保留兼容性；新生产流程应优先调用 Runtime 的 `football_data(action=evidence)`。
