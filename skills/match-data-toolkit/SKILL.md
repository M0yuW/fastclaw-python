---
name: match-data-toolkit
description: 足球赛事数据工具包。ESPN 脚本通过受信赛事映射支持世界杯、欧冠及常用联赛；其余旧脚本暂保留世界杯兼容能力。时间统一换算北京时间。
license: Apache-2.0
env:
  - name: ODDS_API_KEY
    description: The Odds API key（odds_data.py 用；不设则赔率脚本不可用，回退 web_search）
    required: false
---

# 足球赛事数据工具包

Python 仓库维护的赛事数据工具包。`espn_data.py` 不再固定世界杯，调用时必须显式给出
受支持的赛事名称；未知 ESPN slug 会被拒绝。其他脚本仍保留原世界杯兼容语义。

## 可用工具

所有脚本位于 `scripts/`，从技能根目录运行。

| 命令 | 用途 |
|------|------|
| `python3 scripts/match_data.py --schedule --date 2026-06-30` | 某天的世界杯赛程（含已赛比分） |
| `python3 scripts/match_data.py --results --season 2026` | 某届全部赛果 |
| `python3 scripts/match_data.py --form Brazil` | 球队近况（最近若干场） |
| `python3 scripts/match_data.py --h2h Brazil Argentina` | 两队历史交锋 |
| `python3 scripts/match_data.py --standings --season 2026` | 小组排名 |
| `python3 scripts/espn_data.py --list-competitions` | 列出受信 ESPN 赛事映射 |
| `python3 scripts/espn_data.py --competition "瑞典超" --schedule --date 2026-08-10` | 指定赛事的赛程/比分/event id |
| `python3 scripts/espn_data.py --competition "瑞典超" --match "Sirius" "Brommapojkarna" --date 2026-08-10` | 指定赛事的单场 summary |
| `python3 scripts/espn_data.py --competition "FIFA World Cup" --summary --event 760496` | 指定赛事的 event summary |
| `python3 scripts/espn_data.py --competition "FIFA World Cup" --discipline "Egypt" --event 760499` | 同赛事历史场次的黄/红牌事件 |
| `python3 scripts/odds_data.py --list` | 列出有赔率的世界杯赛事 + 剩余 API 额度 |
| `python3 scripts/odds_data.py --match "France" "Sweden"` | 单场赔率（胜平负+大小球）+ 隐含概率 |
| `python3 scripts/sporttery_data.py --list-worldcup` | 列出体彩竞彩可售世界杯赛事及 HAD/HHAD SP |
| `python3 scripts/sporttery_data.py --match "Colombia" "Ghana"` | 查单场体彩胜平负/让球胜平负 SP |
| `python3 scripts/sporttery_data.py --ev "Colombia" "Ghana" --model-prob 66,23,11` | 用模型概率计算体彩 HAD 去水概率与 EV |
| `python3 scripts/hit_rate.py --by-date` | 读账本算命中率（本系统 vs 抛硬币 vs Opta/官方） |
| `python3 scripts/ledger_report.py --full` | 稳定展示账本全量预测表，避免模型手工整理漏行 |
| `python3 scripts/ledger_report.py --pending` | 稳定展示待结算预测表 |

## 预测账本（ledger.json）

coordinator 每跑完一轮日报，把每场预测以一行 JSON 追加到账本（默认 `~/.fastclaw/worldcup/ledger.json`，可用环境变量 `WC_LEDGER` 覆盖）。次日复盘时回填 `actual_result`，`hit_rate.py` 即可统计命中率。账本行格式：
```json
{"date":"2026-06-30","match":"France vs Sweden","our_pred":"France",
 "our_confidence":"high","baselines":{"opta":"France"},"actual_result":null}
```
- `our_pred`：融合后的判断（主队名 / 客队名 / "draw"）。
- `actual_result`：预测时为 `null`，复盘时回填真实胜者或 "draw"。
- `baselines`：可选，记录 Opta/官方等外部预测，供对比命中率。

## 赔率取数（odds_data.py）

`odds_data.py` 封装 The Odds API（胜平负 h2h + 大小球 totals，不取让球/亚盘）。**key 从环境变量 `ODDS_API_KEY` 读，脚本不硬编码**，可安全提交。免费额度有限（500 次），每次输出带 `requests_remaining`。**调用前应优先用 `web_search` 找公开赔率，本脚本作为第二层精确来源。**

## 输出格式

所有脚本以 **JSON** 输出到标准输出，错误到标准错误。接口/网络失败时返回 `{"error": ..., "note": "fall back to web_search"}`，调用方应据此回退到 `web_search` 获取信息。

## 注意事项

- **事实先行**：预测前必须先用 `--schedule` / `--results` 核对当天踢哪几场、已赛真实比分。
- **免费 key 数据浅**：`--form` / `--h2h` 在免费 key 下只能拿到最近少量比赛；当日伤停、首发、天气等临场信息 TheSportsDB **不提供**，须用 `web_search` / `web_fetch` 补充并标注「未核实」。
- `espn_data.py --discipline` 只汇总 ESPN `keyEvents` 里的球员级黄/红牌；累计黄牌是否导致停赛必须另查赛事规则、球队公告或可靠新闻，不能只凭牌数断言 confirmed suspension。
- 时间已换算北京时间（`kickoff_bj` 字段）。
