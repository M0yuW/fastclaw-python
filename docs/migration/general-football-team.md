# 通用足球赛事分析 Team

## 目标

`football-competition-analysis` 是世界杯模板的通用版本，适用于国家队或俱乐部的联赛、
杯赛和两回合淘汰赛。它不修改 `world-cup-analysis`、现有世界杯 Agent、Skill 或账本。

团队包含一个 coordinator 和六个相互独立的 specialist：赛事数据、战术与阵容、赔率、
历史、风险、EV 校准。创建前无需安装世界杯专用 Skill；specialist 使用内建
`football_data` 获取结构化基础数据，并以 `web_fetch` 补充已知公开 URL。

Coordinator 在委派前必须确定：

- competition；
- season/edition；
- stage/round；
- fixture、kickoff、timezone、venue；
- 单场、两回合及 aggregate/extra-time 规则。

信息不足时先向用户澄清。所有 specialist 输出必须带 `as_of`、赛事范围和来源；不同赛事、
赛季或同名球队的数据不得混合。

## Runtime 改动

- 新增公开模板 `football-competition-analysis`；Teams 页面会通过模板 API 自动显示，无需
  前端硬编码。
- 新增 `football_ledger`，以 `(competition, date, match)` 为唯一键；支持 append、settle、
  按 competition/season 筛选 report，以及 `pending_only`。
- 账本固定在当前 coordinator 的
  `~/.fastclaw-python/workspaces/<agent_id>/football/ledger.json`，与世界杯
  `worldcup/ledger.json` 完全隔离。
- `worldcup_ledger` 保持名称、schema 和路径不变，避免破坏历史会话和已有生产 Agent。
- `spawn_subagent` 使用独立 120 秒执行预算；普通工具仍保持 30 秒。日志记录 root、source、
  target、取消阶段和持续时间，不记录任务正文或凭据。
- 通用足球 coordinator 首轮工具全部失败时由 Runtime 直接终止，不再让模型循环重试或用
  训练记忆替代实时证据。
- 缺少对阵、赛事名称或日期/赛季/轮次时，Runtime 在 Provider 调用前直接要求用户澄清。
- Team 创建 API 支持独立 `specialistModel`；推荐 coordinator 使用推理模型、specialist 使用
  低延迟模型。

## Prompt 调整

世界杯 prompt 中以下内容不能复制到通用模板：

- “2026 世界杯”、FIFA 小组赛或淘汰赛的固定表述；
- 固定的世界杯 Agent ID；
- `~/.fastclaw/.../worldcup/ledger.json`；
- 默认世界杯停赛、赛制和时区假设；
- 把国家队历史直接用于俱乐部赛事，或把不同赛事的 form/standings 混合。

通用模板改为由 Team roster 提供 specialist ID，并要求 competition/season/stage 作用域、
事实与推断分离、来源时间戳、两回合规则及数据缺失的显式降级。

## 世界杯脚本为何不能直接跨赛事

底层数据源并非都只覆盖世界杯，限制主要来自脚本中的固定赛事标识：

- `match_data.py` 固定 TheSportsDB `WC_LEAGUE_ID = 4429`；
- `espn_data.py` 固定 ESPN slug `fifa.world`，discipline 还按 `world cup` 字符串过滤；
- `odds_data.py` 固定 The Odds API sport key `soccer_fifa_world_cup`；
- `sporttery_data.py --match` 已经跨赛事，只是列表命令固定筛选“世界杯”；
- ledger/report 默认路径和统计口径也固定为 World Cup。

直接删除这些常量会产生同名联赛、赛季和 event 混用风险，因此本轮新增内建
`football_data`，先用 competition + country 显式搜索获得 `league_id`，再查询指定联赛和赛季。
TheSportsDB 免费 v1 的联赛列表有条数限制，因此未提供 country 时结果会明确标记可能截断。
当前支持：

- competition search；
- 指定 league/date 的 schedule；
- 指定 league/season 的 results 与 standings；
- team form 和浅层 H2H；
- 体彩当前全赛事列表中的精确 match 查询。

仍需继续参数化的能力包括：

1. `espn_data.py`
   - 将固定 `fifa.world` 改为显式 `--league` slug；
   - discipline 回溯按所选 competition 过滤，不再搜索字符串 `world cup`；
   - 每个 event 输出实际 league slug，失配时 fail closed。
2. `odds_data.py`
   - 将固定 `soccer_fifa_world_cup` 改为 allowlisted `--sport`；
   - 先列出 The Odds API sports catalog，再由可信配置选择 key；
   - 保留 quota 可见性，不允许模型提供 API key。
3. `hit_rate.py` / `ledger_report.py`
   - 使用 `FOOTBALL_LEDGER`，并按 competition/season 分组；
   - 保留 `WC_LEDGER` 兼容路径，但不得把两类账本自动合并。
4. `SkillScriptTool`
   - 为通用 Skill 注入 `FOOTBALL_LEDGER`；
   - 继续由 Runtime 注入路径，禁止模型传入任意账本路径。

The Odds API 和 ESPN 参数化完成并有固定 HTTP fixture、competition mismatch、歧义联赛、
空数据和 quota 测试后，再决定是否发布独立 Skill 并开放受限 `exec`。内建 `football_data`
不接受 API key 参数，也不会访问旧 Go workspace。
