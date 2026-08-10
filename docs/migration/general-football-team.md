# 通用足球赛事分析 Team

## 目标

`football-competition-analysis` 是世界杯模板的通用版本，适用于国家队或俱乐部的联赛、
杯赛和两回合淘汰赛。它不修改 `world-cup-analysis`、现有世界杯 Agent、Skill 或账本。

团队包含一个 coordinator 和六个相互独立的 specialist：赛事数据、战术与阵容、赔率、
历史、风险、EV 校准。创建前无需安装世界杯专用 Skill；首版只开放 `web_fetch`，避免把
世界杯硬编码脚本产生的数据误用于其他赛事。

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

## Prompt 调整

世界杯 prompt 中以下内容不能复制到通用模板：

- “2026 世界杯”、FIFA 小组赛或淘汰赛的固定表述；
- 固定的世界杯 Agent ID；
- `~/.fastclaw/.../worldcup/ledger.json`；
- 默认世界杯停赛、赛制和时区假设；
- 把国家队历史直接用于俱乐部赛事，或把不同赛事的 form/standings 混合。

通用模板改为由 Team roster 提供 specialist ID，并要求 competition/season/stage 作用域、
事实与推断分离、来源时间戳、两回合规则及数据缺失的显式降级。

## 结构化脚本的后续参数化

现有 `match-data-toolkit` 仍是世界杯专用，不应绑定到通用模板。达到世界杯模板的数据能力
对等前，需要发布独立的 `football-data-toolkit`，至少完成：

1. `match_data.py`
   - 删除固定 `WC_LEAGUE_ID = 4429`；
   - 使用明确的 `--league-id` 或受控 competition catalog；
   - 输出中固定包含 competition ID/name、season 和 source；
   - 不根据用户文本模糊选择同名联赛。
2. `espn_data.py`
   - 将固定 `fifa.world` 改为显式 `--league` slug；
   - discipline 回溯按所选 competition 过滤，不再搜索字符串 `world cup`；
   - 每个 event 输出实际 league slug，失配时 fail closed。
3. `odds_data.py`
   - 将固定 `soccer_fifa_world_cup` 改为 allowlisted `--sport`；
   - 先列出 The Odds API sports catalog，再由可信配置选择 key；
   - 保留 quota 可见性，不允许模型提供 API key。
4. `sporttery_data.py`
   - 用 `--list --competition <name>` 替代 `--list-worldcup`；
   - league 匹配必须返回候选和精确命中状态，歧义时不继续计算 EV。
5. `hit_rate.py` / `ledger_report.py`
   - 使用 `FOOTBALL_LEDGER`，并按 competition/season 分组；
   - 保留 `WC_LEDGER` 兼容路径，但不得把两类账本自动合并。
6. `SkillScriptTool`
   - 为通用 Skill 注入 `FOOTBALL_LEDGER`；
   - 继续由 Runtime 注入路径，禁止模型传入任意账本路径。

脚本参数化完成并有固定 HTTP fixture、competition mismatch、歧义联赛、空数据和 quota 测试后，
再把 `football-data-toolkit` 加入模板的 `skills.alwaysLoad`，并开放受限 `exec`。在此之前，
preview 不应声称这些结构化数据源可用。
