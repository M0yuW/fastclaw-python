# 足球分析链路交接手册

- 最后更新：2026-08-16（Asia/Shanghai）
- Python 仓库：`https://github.com/M0yuW/fastclaw-python`
- 当前运行工作树：`/Users/m0yuw/.codex/worktrees/8e65/fastclaw/football-team`
- 当前分支：`codex/general-football-team`
- 最新提交：`c888844 Stop redundant football evidence retries`
- Gateway：`http://127.0.0.1:18954`
- 数据库：`/Users/m0yuw/.fastclaw-python/fastclaw.db`
- LaunchAgent：`com.fastclaw.python.local`

本文是足球分析功能的专项交接补充。通用 Python 迁移信息仍以同目录的
`HANDOFF.md` 和 `docs/migration/` 为准。

## 1. 当前结论

足球分析链路已经完成一轮修复并推送远端。最近一次完整测试结果：

```text
265 passed, 1 skipped, 2 warnings
```

Gateway 已重启，健康检查结果：

```json
{"status":"ok","state":"running"}
{"status":"ready","state":"running","checks":{"database":true,"agent_manager":true,"providers":true,"skills":true,"plugins":true}}
```

## 2. 已发现的问题与处理结果

| 问题 | 根因 | 当前状态 |
|---|---|---|
| 新会话仍使用旧配置 | 运行态和持久化会话没有统一恢复基础证据；旧 prompt 还会继续发起旧动作 | 已增加持久化基础证据恢复和旧动作兼容保护 |
| 四个分析师重复请求 TheSportsDB | 每个 specialist 都独立查询，没有共享 machine-readable football base evidence | data analyst 先发布一次 `base_evidence`，其他角色读取共享结果 |
| 数据分析/赔率分析调用 Sporttery | 来源职责混淆 | data/odds 禁止 Sporttery；Sporttery 只给用户明确要求的 EV analyst |
| 赔率分析没有有效请求 | 未先读取 `football_context`，或球队别名导致 The Odds API 结果无法匹配 | odds analyst 先读 context；球队身份统一解析；`no_match` 与未请求分开 |
| `Deportivo Alavés`、`FC Utrecht`、`SBV Excelsior` 等匹配失败 | 中文、英文、重音、俱乐部前缀没有统一 canonical identity | 已加入统一球队身份解析、别名和 provider ID 逻辑 |
| Racing Santander 赔率 `no_match` | Odds API 返回 `Real Racing Club de Santander`，本地身份曾解析成不同 canonical ID | 已修复并加回归测试；不能再直接认定为“接口无报价” |
| 第一轮没有状态、阵容、H2H | 只看当前赛季；新赛季第一轮本来没有当前赛季样本 | historical context 现在区分上赛季正式赛、升降级边界、热身赛和 H2H；缺失仍必须明确标注 |
| 桑坦德升班马被当作普通西甲球队 | 没有识别跨级别升降级边界 | 已增加 `prior_division_matches` 和 `competition_boundary`，分析时必须说明“西乙升班马 vs 西甲球队” |
| H2H 空数组 | TheSportsDB 没有可用的专用 H2H endpoint；近期事件也可能不含对手 | 已通过 documented event search 双向检索上一赛季正式比赛，并去重 |
| 失败时统一返回“所有数据源缺失” | 编排错误、HTTP 失败、超时、空结果、未匹配没有分层 | 应分别显示工具名、错误类别、影响范围；基础证据有效时继续生成低置信度结果 |
| `football_ledger` 重复记账 | “西甲”和“La Liga”以及球队别名被当成不同比赛 | 账本使用 canonical competition/date/home/away；已有记录走 `update`，不是重复 `append` |
| 更新账本被误报成重复记账 | prompt 只会 append，不理解 ledger 的 update 语义 | `existing_requires_update` 后必须调用 `update`；最终用 `report` 输出 Markdown 表格 |
| 前端工具参数显示 `[object Object]` | 嵌套参数直接被字符串化 | 工具摘要显示为 `update · 西甲 · 阿拉维斯 vs 赫塔费`，展开详情才显示完整 JSON |
| 复盘账本直接返回账本 | coordinator 把普通对话窄化成预测或只调用 report | 普通足球对话不应强制补充比赛范围；复盘时先 `results` 核验已结束比分，再 update/report |
| 子 agent 超时或参数错误 | 历史分析 prompt 让 specialist 重复调用不适合的工具；并行任务失败被包装过度 | 已增加来源职责、data-first 和错误分层；剩余外部 provider 超时仍需按具体错误说明 |

## 3. 正确的足球分析流程

### 3.1 普通对话与预测入口

足球 coordinator 不能把所有消息都识别成“预测请求”。以下内容应直接回答或进入相应流程：

- 解释工具、配置、数据源或会话状态；
- 复盘已有预测和账本；
- 查询比赛是否结束、核对比分；
- 修正账本、查看报告；
- 用户只是在澄清球队、赛事或历史数据。

只有用户明确要求预测/分析时，才进入预测编排。不要一律回复：
“请先补充对阵双方、赛事名称、比赛日期、赛季或轮次。”

### 3.2 新会话

1. coordinator 识别比赛范围和用户意图。
2. data analyst 对每场比赛调用一次：
   `football_data(action=base_evidence)`。
3. Runtime 必须发布 machine-readable football base evidence 后，才允许其他 specialist 执行。
4. odds analyst 读取 `football_context`，再调用 The Odds API。
5. 战术、历史、风险分析师读取共享基础证据，不重复调用 TheSportsDB。
6. 默认预测流程包含赔率分析；只有用户明确排除赔率时才使用 `odds_not_requested`。
7. EV analyst 默认不启动；只有用户明确要求 EV、Sporttery、赔率价格或敏感性分析时启动。
8. coordinator 汇总证据、缺失项、方向、置信度、失效条件，并按固定 Markdown 表格输出。

### 3.3 继续已有会话

- 优先读取当前运行态的共享 base evidence；
- gateway 重启后从持久化 `football_data` 结果恢复；
- `evidence_schema_version` 当前为 `3`；旧 v2 bundle 应刷新；
- base evidence 已存在时，不能再次按上赛季日期查询当前 fixture；
- H2H、上赛季正式赛、升降级边界和热身赛应从 `historical_context` 读取。

## 4. 数据源职责

| 角色 | 允许来源 | 禁止/注意 |
|---|---|---|
| 数据分析师 | TheSportsDB，ESPN 补充 | 不调用 Sporttery；只负责赛程、赛事、状态、积分、历史基础资料 |
| 赔率分析师 | The Odds API | 先读 `football_context`；不调用 Sporttery；记录是否真正发起请求、返回状态和报价数量 |
| EV 分析师 | Sporttery | 仅用户主动要求 EV/赔率价格/敏感性分析时启动 |
| 历史分析师 | 共享 `historical_context` | 不为 H2H 单独重新查询当前 fixture；首轮必须使用跨赛季标注 |
| 战术分析师 | 共享基础证据及允许的战术资料 | 外部请求超时不能冒充“数据全部缺失” |

工具状态必须区分：

```text
not_requested / empty / unavailable / rejected / no_match / success
```

`no_match` 只表示请求成功但 provider 没有匹配事件；不能在没有记录请求参数和返回内容时臆断是接口没有报价，也不能忽略身份匹配问题。

## 5. 最近一次具体会话问题

会话：`s-1786880756034-fgztn8`

该会话中两场当前比赛的 `base_evidence` 已成功发布。之后数据 analyst 又发起了类似以下的冗余调用：

```json
{
  "action": "evidence",
  "competition": "La Liga 2",
  "season": "2025/26",
  "date": "2025-08-16",
  "team_a": "Racing Santander",
  "team_b": "Villarreal"
}
```

这造成：

- `primary_fixture_not_on_requested_date`：把历史日期当成当前赛程日期；
- `ESPN action requires a competition...`：实际是 `La Liga 2` 不在可信顶级赛事目录，且旧文案错误地把所有 `_trusted_competition` 失败都称为 ESPN 错误。

本次修复后：

- data role 的工具 schema 不再暴露 `evidence`；
- 旧会话若仍调用 `evidence`，在已有 base evidence 时返回 `base_already_published`，不会再次发请求；
- prompt 明确要求 base evidence 成功后停止调用 football_data；
- `_trusted_competition` 错误改为通用 `football_data requires a competition...`。

继续该会话可发送：

```text
继续当前会话。两场比赛的 base_evidence 已成功，不要再按历史日期调用 football_data evidence，也不要重复验证相邻级别赛事。直接读取现有 historical_context，继续历史分析、赔率分析和最终预测。
```

## 6. 账本规则

唯一键：

```text
canonical_competition + date + canonical_home + canonical_away
```

规则：

1. 修改前先备份账本。
2. `append` 返回 `existing_requires_update` 时必须改用 `update`。
3. 同一场比赛不允许因为赛事别名、重音、前缀或中英文名称产生新行。
4. 预测冲突时保留最新预测，同时在备注中保留旧记录信息。
5. 复盘已结束比赛时，只能在 `football_data(action=results)` 验证最终比分后结算。
6. 面向客户直接输出 Markdown 表格，不返回让客户下载的 JSON。
7. 最终应调用 `football_ledger(operation=report)`，并在回复中明确已新增、已更新、已结算或未变更。

## 7. 当前代码与验证

本次最新提交涉及：

- `src/fastclaw/tools/football_data.py`
- `src/fastclaw/teams.py`
- `tests/test_football_evidence.py`

关键修改：

- data role 只提供 `base_evidence`/`results`；
- 兼容旧会话的重复 `evidence` 调用；
- 修复 trusted competition 错误文案；
- 增加历史日期冗余调用回归测试。

验证命令：

```bash
cd /Users/m0yuw/.codex/worktrees/8e65/fastclaw/football-team
.venv/bin/ruff check --no-cache src tests
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q --disable-warnings
```

预期：`265 passed, 1 skipped`。

## 8. Gateway 操作

查看状态：

```bash
launchctl print gui/$(id -u)/com.fastclaw.python.local
tail -n 120 /Users/m0yuw/.fastclaw-python/gateway.log
```

重启：

```bash
launchctl kickstart -k gui/$(id -u)/com.fastclaw.python.local
```

检查：

```bash
curl --fail --silent --show-error http://127.0.0.1:18954/healthz
curl --fail --silent --show-error http://127.0.0.1:18954/readyz
```

LaunchAgent 使用的关键环境：

```text
PYTHONPATH=/Users/m0yuw/.codex/worktrees/8e65/fastclaw/football-team/src:...
FASTCLAW_DATABASE_URL=sqlite+aiosqlite:////Users/m0yuw/.fastclaw-python/fastclaw.db
FASTCLAW_DATA_ROOT=/Users/m0yuw/.fastclaw-python
FASTCLAW_PORT=18954
```

不要手工修改数据库中的 provider key，不要把 `provider.env`、SQLite 数据库或 token 提交到 Git。

## 9. 新会话开场提示词

```text
请先阅读 /Users/m0yuw/Downloads/fastclaw-complete-device-migration-2026-08-11/fastclaw-python/FOOTBALL-ANALYSIS-HANDOFF.md。

这是足球分析链路的继续工作。不要重新引入旧逻辑：
1. 普通足球对话不强制识别成预测；
2. 新预测先由 data analyst 对每场调用一次 football_data action=base_evidence；
3. base evidence 成功后停止重复调用 football_data；
4. odds analyst 只用 The Odds API，EV analyst 只有用户主动要求才启动；
5. 历史分析读取 historical_context，首轮要考虑上赛季正式比赛、升降级边界和热身赛；
6. 账本按 canonical fixture update，不重复 append；复盘先用 results 核验完赛比分；
7. 工具失败必须区分具体错误，不要统一回复“所有数据源缺失”。

先检查当前分支、gateway health/ready 和会话状态，再继续用户的新任务。
```

