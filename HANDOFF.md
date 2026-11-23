# FastClaw Go → Python 迁移交接手册

- 最后更新：2026-08-09（Asia/Shanghai）
- Python 仓库：<https://github.com/M0yuW/fastclaw-python>
- Go 行为基线：`M0yuW/fastclaw@792417b86b5c12af1b99364865217a74f4d52f38`
- 本地工作树：`/Users/wangzheyu/.codex/worktrees/8e65/fastclaw/fastclaw-python`
- 当前工作分支：`codex/agent-teams-web@135d18a`
- `main` / `origin/main`：`80a1c82`（PR #1–#15 已合并）

本文是当前迁移工作的事实入口。阶段文档记录当时的设计和验收过程；若其中的分支、
凭据或运行状态与本文冲突，以本文和实时命令输出为准。

---

## 1. 当前目标

### 1.1 近期目标

将 Coordinator 团队功能按堆叠 PR #17 → #18 → #19 合入 `main`，随后在 18954 启动
Gateway，由产品使用者验证以下路径：

1. 原 M0yuW 与 benchmark 身份、27 个迁移 Agent 和历史 Session 继续可用；
2. 使用已补齐的 benchmark finance 8 人映射修复现有 6 人团队后，生产金融、世界杯、
   benchmark finance、benchmark runtime 四个团队可完整展示；
3. 用户可从金融/世界杯模板或 Custom 创建 coordinator + 1–12 个 specialist；
4. coordinator 只能委派给同租户、同团队、active specialist；
5. 归档立即拒绝新执行并取消相关 root execution；恢复后重新注册；
6. 永久删除必须显式确认，并删除团队专属 Agent；
7. Provider 密钥只由 Gateway 环境提供，浏览器和数据库均不保存明文。

### 1.2 最终迁移目标

- Python Runtime 接管生产 Gateway，并最终监听原 Go 端口 18953；
- Go 仓库、二进制、数据库和历史保持只读回退，不复制 Git 历史；
- Go 与 Python 永不共写一个 SQLite，迁移只允许 Go → Python；
- 金融、世界杯和 benchmark coordinator 完成真实 Provider 固定任务；
- Stop 能取消 Provider、工具进程和内部委派，不落库残缺 assistant；
- 完成后再迁移 channels、cron、agent bindings、MCP、sandbox、object store 等非首切能力；
- Runtime API v1 冻结后再创建独立 `fastclaw-eval-python`。

---

## 2. PR、分支和合并状态

### 2.1 已合并基线

PR #1–#15 已进入 `main`：

| PR | 内容 | 状态 |
|---|---|---|
| #1 | Python 3.12+ 项目、Runtime 生命周期、healthz/readyz、基础 CI | MERGED |
| #2 | OpenAI-compatible / Anthropic Provider、流式累计与 `_raw` 契约 | MERGED |
| #3 | SQLAlchemy/Alembic、身份、ACL、Go SQLite 导入 | MERGED |
| #4 | 单 Agent ReAct、Session、工具执行与 SSRF 基础防护 | MERGED |
| #5 | MessageBus、TaskQueue、多 Agent 委派与取消 | MERGED |
| #6 | 带来源说明的 Next.js 快照 | MERGED |
| #7 | Alembic 初始 revision 与漂移门禁 | MERGED |
| #8 | Gateway/Auth/Provider/API 基础接线 | MERGED |
| #9 | `AgentRuntimeManager` 接入 FastAPI lifespan | MERGED |
| #10 | Agent/workspace/skills 资产导入与 Skill 环境准备 | MERGED |
| #11 | 当前 Web 所需管理 API、SSE/Stop/历史兼容 | MERGED |
| #12 | JSON-RPC plugin 与 finance-tools | MERGED |
| #13 | PostgreSQL、package、Docker、安全和发布门禁 | MERGED |
| #14 | Go/Python 双服务 smoke 与证据 | MERGED |
| #15 | 切换审计、委派契约、Agent config precedence 修复 | MERGED |

### 2.2 当前开放堆栈

| PR | Base → Head | 当前提交 | 内容 | 2026-08-09 状态 |
|---|---|---|---|---|
| [#17](https://github.com/M0yuW/fastclaw-python/pull/17) | `main` → `codex/agent-teams-schema` | `cb3ef01` | team schema、模板、原团队回填、幂等创建 | Draft、CLEAN、9/9 CI 绿；benchmark finance 8 人修复已完成 |
| [#18](https://github.com/M0yuW/fastclaw-python/pull/18) | `agent-teams-schema` → `agent-teams-runtime` | `57a9858` | Team API、Runtime roster/权限、归档/删除/成员生命周期 | Draft、CLEAN、9/9 CI 绿 |
| [#19](https://github.com/M0yuW/fastclaw-python/pull/19) | `agent-teams-runtime` → `agent-teams-web` | `135d18a` | Teams UI、Provider 引导、preview、Web overlay | Draft、CLEAN、9/9 CI 绿 |

三条 PR 当前没有 GitHub review approval 或 review comment。9 个 job 为 Python
3.12/3.13/3.14 quality、Web、PostgreSQL、package、Docker、security、Playwright。

合并顺序固定为 `#17 → #18 → #19`。父 PR 合并后把子 PR retarget 到 `main` 并重跑
全部 CI。不要 rebase、force push 或改写已有提交，以保留审查锚点。

---

## 3. 已完成内容

### 3.1 Runtime 与 Provider

- Python 3.12+、FastAPI、Pydantic v2、HTTPX、AnyIO/asyncio、Uvicorn；
- Provider Protocol 支持 start/stop/ready/chat/stream；
- OpenAI-compatible 与 Anthropic Messages 实现；
- `chat()` 与耗尽 `stream()` 共用 accumulator；
- usage、cache read/write token、Thinking/signature、ToolCall、RawAssistant 可回放；
- Provider EOF、HTTP 错误、畸形 SSE 和取消具有结构化错误语义。

### 3.2 数据、身份与 Go 导入

- SQLAlchemy 2 async Repository/UoW 与 Alembic；SQLite WAL/foreign keys/busy timeout；
- bcrypt 密码、SHA-256 API key 与 Agent ACL 兼容 Go fixture；
- Go 数据库通过只读快照和 SQLite backup 单向导入；
- dry-run、重复导入、孤儿 reject/quarantine、脱敏报告和回滚已实现；
- 资产导入只复制有效 Agent/workspace/shared skills，带 SHA-256 与冲突检查；
- 当前数据：2 用户、27 Agent、892 Session、54 Agent 文件、21 配置。

### 3.3 Agent、工具和多 Agent

- `AgentRunner` 每轮只调用一次 Provider，完整回复后才持久化；
- 文件、受限 exec、WebFetch、spawn_subagent、World Cup ledger 已接入；
- exec 使用绝对路径、inode 校验、受控环境、进程组 TERM/KILL 和输出上限；
- WebFetch 逐跳拒绝私网地址，并以 DNS pin transport 缩小重绑定窗口；
- MessageBus/TaskQueue 支持 per-target FIFO、全局并发、backpressure、环检测、去重、取消和 shutdown；
- plugin 可信身份由 Runtime 注入，finance-tools 状态按租户隔离。

### 3.4 Gateway、Web 和发布

- Cookie 登录、Bearer API key、只读 actAs、管理员用户/Agent API；
- `/api/chat`、SSE v2、`/v1/chat/completions`、`/v1/agents`；
- Agent/config/files/system files、Session、Provider、API key、tools/tasks/plugin API；
- Next.js 静态导出由 FastAPI 提供，并使用 overlay manifest 记录 Python 改动；
- wheel/sdist、非 root Docker、PostgreSQL、Playwright、pip-audit、gitleaks CI；
- Go 18953 / Python 18954 differential harness 和 disposable smoke 已建立。

### 3.5 Coordinator 团队（开放 PR，尚未进入 main）

- `agent_teams` / `agent_team_members` 与 Agent 全局唯一团队归属；
- 团队创建原子事务、`clientRequestId` 幂等、`revision` 乐观并发；
- 生产金融 5 人、世界杯 7 人、benchmark finance 8 人、benchmark runtime 5 人模板已对齐；
- 对现有 27 Agent 的四个团队精确回填，不更改 Agent ID、Session、SOUL 或文件；
- PR #17 可将已有 benchmark finance 6 人团队原地、幂等补为 8 人；不会创建新 Agent；
- 当前 Python live 数据库仍是修复执行前的 4 个 active team、23 个 member；执行修复后应为
  25 个 member，仅 LEO 和 Finance Solo 保持 standalone；
- Team preview 检查 Provider、model、skills 和 tools，create 对未满足前置条件 fail closed；
- coordinator 的 `spawn_subagent.agent_id` 被收窄为本团队 active specialist enum；
- 运行时再次验证 tenant/team/member/status，独立 coordinator 不能调用团队 specialist；
- 支持团队更新、成员新增/归档、团队归档/恢复和显式永久删除；
- Teams UI 支持模板、自定义 specialist、Provider profile、preview、归档/恢复/删除。

---

## 4. 关键技术决策

| 主题 | 已锁定决策 |
|---|---|
| 仓库历史 | Python 仓库独立初始化；Go 仓库只读，不复制其 Git 历史 |
| 数据方向 | 只支持 Go → Python；Python 数据不得回写 Go |
| 数据目录 | Python 固定使用独立 `~/.fastclaw-python`；禁止两个 Runtime 共写 SQLite |
| Go 源库 | Importer 不修改源库；DB/WAL/SHM 作为集合复制后再 backup；禁止 `immutable=1` |
| 身份 | `user_id/agent_id/session_id/root_execution_id/call_path` 来自可信 context，不接受模型伪造 |
| Provider 密钥 | 仅环境变量 `FASTCLAW_PROVIDER_<NAME>_API_KEY`；数据库只保存非敏感 endpoint/model/config |
| 配置优先级 | system/user defaults → `agent.json` → Agent-scoped defaults → `agents.config` |
| ToolCall `_raw` | Python 本地 raw 先归一化 ID；Provider 权威 raw 只校验、不改写，冲突 fail closed |
| Team 归属 | specialist 为团队专属；一个 Agent 最多属于一个团队；一个团队恰好一个 coordinator |
| Team 委派 | coordinator 只能调用同团队 active specialist；specialist v1 不拥有 `spawn_subagent` |
| Team 生命周期 | 默认归档；永久删除需 team ID + revision 确认，并删除专属 Agent |
| Skill | 仅本地 catalog；依赖由 `fastclaw skills prepare` 显式安装，运行请求中禁止安装 |
| Web 来源 | Go `792417b` 快照 + 明确 Python overlay 清单和 SHA-256，不复制 Go 历史 |
| 回滚 | 停 Python、恢复 Go 18953；Python 新数据只留作审计，不反向迁移 |

---

## 5. 修改过的核心文件

### 5.1 已合并 Runtime 基线

| 文件/目录 | 责任 |
|---|---|
| `src/fastclaw/app.py` | FastAPI factory、lifespan、healthz/readyz、Web 静态挂载 |
| `src/fastclaw/runtime.py` | Provider 生命周期、共享 HTTP client、启动回滚和 shutdown |
| `src/fastclaw/providers/` | Provider Protocol、模型、OpenAI/Anthropic、SSE accumulator |
| `src/fastclaw/storage/` | SQLAlchemy models、records、repositories、UoW、数据库初始化 |
| `src/fastclaw/migration/` | Go DB 快照、schema 映射、脱敏、导入和报告 |
| `src/fastclaw/agent/runner.py` | ReAct loop、流式事件、工具调用与最终持久化 |
| `src/fastclaw/agent/manager.py` | Agent profile、Provider/Skill/Tool 装配、队列、取消与 delegated chat |
| `src/fastclaw/orchestration/` | MessageBus、TaskQueue、waiter、去重、batch、环检测 |
| `src/fastclaw/tools/` | 文件、exec、WebFetch、Skill script、World Cup ledger |
| `src/fastclaw/gateway/router.py` | Auth、Web API、OpenAI API、SSE/Stop、管理面 |
| `src/fastclaw/plugin/` | JSON-RPC plugin 协议、进程监督、可信参数注入 |
| `plugins/finance-tools/` | 金融工具 Python 移植、租户隔离状态和来源说明 |
| `scripts/cutover_*.py` | disposable wiring、真实业务 smoke 与切换审计 |

### 5.2 当前 Team 堆栈

| 文件 | 修改内容 |
|---|---|
| `migrations/versions/20260806_01_agent_teams.py` | Team 与 member 初始表 |
| `migrations/versions/20260806_02_unique_team_member_agent.py` | `agent_id` 全局唯一团队归属 |
| `src/fastclaw/teams.py` | 模板、角色、原子创建、幂等、specialist 新增、preview 前置条件 |
| `src/fastclaw/cli.py` | `fastclaw migrate backfill-teams`、dry-run 和审计 manifest |
| `src/fastclaw/agent/manager.py` | roster prompt、动态 enum、团队授权、active root 取消 |
| `src/fastclaw/orchestration/tool.py` | 对团队 coordinator 收窄 `spawn_subagent` target schema |
| `src/fastclaw/gateway/models.py` | camelCase Team 请求模型 |
| `src/fastclaw/gateway/router.py` | Team preview/CRUD/member/lifecycle API |
| `web/src/app/teams/page.tsx` | Team 创建与生命周期 UI |
| `web/src/lib/api.ts` | Team 和 Provider 浏览器 API |
| `tests/test_teams.py` | schema、幂等、精确回填、dry-run、模板前置条件 |
| `tests/test_agent_manager.py` | roster enum、跨团队拒绝、归档/取消 |
| `tests/test_gateway.py` | Team API 幂等、revision、成员和删除生命周期 |

---

## 6. 当前数据、凭据和运行状态

### 6.1 数据库

当前数据库：`~/.fastclaw-python/fastclaw.db`

| 项 | 当前值 |
|---|---:|
| Alembic revision | `20260806_02` |
| 用户 | 2 |
| Agent | 27 |
| Session | 892 |
| Agent 文件 | 54 |
| 配置 | 21 |
| Team | 4 active |
| Team member | 23（应修正为 25） |

重要：`main@80a1c82` 不包含 `20260806_01/02` migration，但当前数据库已经升级到
`20260806_02`。在 PR #17 合并前，不要用旧 `main` 对这个数据库启动 Gateway，也不要
手工降级或删除 `alembic_version`。需要运行时应使用当前 `codex/agent-teams-web` 分支，
或先复制数据库到 disposable 目录。

### 6.2 Provider 与 Skill

`~/.fastclaw-python/provider.env` 当前核对结果（只报告状态，不记录值）：

- DeepSeek：configured，来源为 system config；
- ODDS：configured，且世界杯 Skill 需要；
- OpenRouter：未配置，但当前 27 个 profile 不需要；
- `findata-toolkit-cn`、`findata-toolkit-us`、`match-data-toolkit`：required + prepared；
- `serenity-skill`：未准备，但当前不需要。

任何新 Provider 均需在 Gateway 环境增加对应
`FASTCLAW_PROVIDER_<NORMALIZED_NAME>_API_KEY` 并重启，前端只保存 API base/type/name。

### 6.3 服务

2026-08-09 实测 `127.0.0.1:18954` 为 connection refused，Gateway 当前未运行。
不要引用旧的 health/ready JSON 临时文件判断状态。

使用当前 Team 分支启动：

```bash
cd /Users/wangzheyu/.codex/worktrees/8e65/fastclaw/fastclaw-python
pnpm --dir web build

set -a
source ~/.fastclaw-python/provider.env
set +a

FASTCLAW_DATABASE_URL="sqlite+aiosqlite:///$HOME/.fastclaw-python/fastclaw.db" \
FASTCLAW_DATA_ROOT="$HOME/.fastclaw-python" \
FASTCLAW_PORT=18954 \
.venv/bin/uvicorn fastclaw.app:app --host 127.0.0.1 --port 18954
```

启动后先检查：

```bash
curl -i http://127.0.0.1:18954/healthz
curl -i http://127.0.0.1:18954/readyz
```

---

## 7. 测试与验证结果

### 7.1 2026-08-09 本机复跑（当前 `#19` head）

| 命令 | 结果 |
|---|---|
| `.venv/bin/ruff check .` | `All checks passed` |
| `.venv/bin/ruff format --check .` | `118 files already formatted` |
| `.venv/bin/mypy` | `Success: no issues found in 86 source files` |
| `.venv/bin/pytest -q` | `165 passed, 1 skipped, 1 warning` |
| `alembic upgrade head` | 新库依次升级至 `20260806_02` |
| `alembic check` | `No new upgrade operations detected` |
| `scripts/verify_web_snapshot.py` | `81 unchanged + 9 declared overlays + 5 attributed additions` |
| `pnpm --dir web lint` | exit 0 |
| `pnpm --dir web build` | exit 0，生成 26 routes（含 `/teams`） |
| `fastclaw providers check` | 27 profile；DeepSeek/ODDS ready；三项必需 Skill prepared |

本机 PostgreSQL 测试因没有设置 `FASTCLAW_POSTGRES_TEST_URL` 而跳过；三条开放 PR 的
GitHub PostgreSQL job 均通过。唯一 warning 是 FastAPI/Starlette 对
`HTTP_422_UNPROCESSABLE_ENTITY` 名称的弃用提示，不影响当前行为。

### 7.2 回填核对

旧 `d32b6bd` 对当前数据库复制品执行 migration + `backfill-teams --dry-run` 时曾得到：

- 生产金融：5 个既有 Agent，status `existing`；
- 世界杯：7 个既有 Agent，status `existing`；
- benchmark finance：错误地只把 6 个既有 Agent 判为 `existing`；
- benchmark runtime：5 个既有 Agent，status `existing`；
- 其他 user/template 组合按 `no_matching_agents` 明确跳过；
- `Finance Source Specialist` 与 `Finance Trend Analyst` 被留作 standalone，暴露出目标映射不完整；
- dry-run 未写入目标，临时副本已删除。

PR #17 的 `cb3ef01` 已关闭该问题：模板和精确映射包含全部 8 人；已有 6 人团队会在
同一 team 上补两条 membership、revision 增 1，不创建 Agent；重复执行返回 `existing`。
对应修复测试以及 Python 3.12–3.14、PostgreSQL、Web、package、Docker、security、Playwright
共 9 项 GitHub CI 均通过。真实 live DB 尚未执行修复，因此当前仍是 23 个 member。

### 7.3 已有差分/切换证据

`docs/migration/evidence/` 已保留以下脱敏证据：

- 未认证 Go/Python health/status smoke；
- 管理员登录、2 用户、13 + 14 Agent、只读 actAs；
- 认证 chat/SSE、ToolCall/ToolResult 配对、任务终态、慢流取消；
- disposable Runtime wiring 下的金融、世界杯、两个 benchmark coordinator。

这些证明 Runtime 接线和兼容 harness，不等同于当前 Team UI + 真实 DeepSeek 业务验收。

---

## 8. 已知问题与未完成项

1. **Team 功能尚未进入 main。** #17–#19 虽然 CI 全绿，但仍是 Draft、没有 review approval。
2. **live DB 已领先 main。** 当前 revision `20260806_02`，旧 main 不应直接启动该库。
3. **benchmark finance 修复尚未应用到 live DB。** 代码与 CI 已验证，但当前仍有 23 个 member；
   合并后需在备份副本 dry-run，再对 live DB 执行幂等 backfill 并核对为 25。
4. **Gateway 当前未运行。** 18954 connection refused；需按 6.3 节启动后由用户验证。
5. **真实 Team 产品 smoke 未完成。** 尚未记录“从 UI 新建团队 → coordinator 真实委派 →
   archive/restore/delete”的真实 DeepSeek 证据。
6. **Session 标题 Markdown 清洗未修。** GitHub [Issue #16](https://github.com/M0yuW/fastclaw-python/issues/16)
   仍开放；侧边栏可能出现 `#`、`##`、`---`。
7. **Channels 尚未迁移。** channel token/credential_key 导入时被清空；入站路由需重新配置。
8. **Cron、agent bindings、远程 Skill registry、全局 Skill 删除返回结构化 501。** 这是已知
   未实现能力，不是路由丢失。
9. **真实 Provider 故障验证仍不足。** 429/5xx/异常 EOF/真实 cache token 目前主要依赖 mock；
   应在不泄露响应内容的前提下补真实端点证据。
10. **本机 PostgreSQL 未复跑。** 依赖 GitHub CI 的 postgres:17 结果。
11. **阶段 10 文档部分状态已过期。** 其中“DeepSeek/ODDS 未配置”“Gateway 正在运行”等旧描述
    不再准确；本文为当前事实来源。
12. **剩余完整能力未迁移。** MCP、Docker sandbox 产品接线、channels/webhook、croniter、
    object store、usage/quota、daemon/CLI 等仍在首切之后。

---

## 9. 尝试过但失败或被纠正的方案

### 9.1 Team 开发中的失败迭代

- **按名称词根猜测回填团队**：最初用 `china/us/screener`、`match/team/...` 搜索，实际
  `coordinator`/`coordinator-wc` 和 specialist 名称无法可靠命中。已改为锁定的精确名称映射，
  partial/ambiguous match 直接报告 conflict。
- **错误的团队模板**：最初生产金融少一个 screener，世界杯角色与生产 Agent 不一致，benchmark
  复用错误模板；finance benchmark 后续又只纠正到 6 人。`cb3ef01` 已补齐 source/trend，并为
  已存在的 6 人团队增加幂等原地修复，最终规模为 5/7/8/5。
- **只约束 `(team_id, agent_id)`**：不能阻止一个 Agent 同时属于多个团队。已增加
  `20260806_02` 对 `agent_id` 的唯一约束。
- **委派检查 fail-open**：最初只有来源已经是团队成员时才拒绝非法目标，独立 coordinator
  仍可命中团队 specialist。现改为目标属于团队时默认拒绝，且 tool schema 带本团队 enum。
- **归档只注销 profile、永久删除只删 team row**：活动任务仍可能继续，Agent 会变成孤立对象。
  现归档先取消相关 root，永久删除同时删除专属 Agent。
- **空 model 写入 Agent config**：会覆盖 `agents.defaults`。现为空时不写 model，并由 preview
  在创建前验证实际 Provider/model。
- **preview 只返回 `provider: deferred`**：无法阻止创建不可运行团队。现检查 Provider、Skill、
  Tool，并在 create 时重复 fail-closed 检查。
- **前端每次提交生成新 request ID**：响应丢失后重试会重复创建。现打开 dialog 时生成并在
  preview/create 间复用同一个 `clientRequestId`。
- **前端新增页面未更新 Web overlay**：#19 曾因 unexpected file/hash mismatch 失败。现新增文件、
  sidebar/API/skills overlay 均登记原因和 SHA-256。

### 9.2 早期迁移中的失败迭代

- **Agent 配置优先级错误**：旧 `agent.json` 一度覆盖更新的 Agent-scoped DB 配置，误把两个 Agent
  解析为 OpenRouter。#15 已修为 Go 的有效优先级；当前 27 profile 只需要 DeepSeek。
- **Web overlay 只登记路径、不钉哈希**：修改 overlay 后 CI 可静默通过。现每个 overlay 有 SHA-256。
- **把 Go `/v1/agents` 当共同 JSON fixture**：锁定 Go launcher 实际交给 SPA，不能作为差分端点。
  differential 改用双方真实共有路径，并记录接受差异。
- **只靠 `twine check` 判断发行包**：曾把 Web 本地产物打入 sdist，约 105 MiB、21,901 文件。
  现有 build exclude 与 `verify_distribution.py` 文件清单门禁。
- **PR 描述声称验证但未跑精确门禁**：Team 初版曾被 Ruff format、mypy 和 Web snapshot 拦截。
  后续提交统一复跑与 CI 相同的命令；当前开放堆栈已全绿。

---

## 10. 下一步开发顺序

### 第 1 步：收口并合并 Team 堆栈

1. 对 #17 的 `cb3ef01` 做最终 schema/backfill review，确认 live 数据备份和
   `20260806_02` revision；
2. 将 #17 从 Draft 标记 ready，合入 `main`；
3. 将 #18 retarget 到 `main`，重跑 9 个 job，审查 Runtime/API 权限和取消；
4. 合并 #18；
5. 将 #19 retarget 到 `main`，重跑 9 个 job，确认 overlay manifest；
6. 合并 #19；
7. 在最终 main 上复跑第 7 节全部本地门禁；
8. 在 live DB 备份副本上 dry-run benchmark finance 修复，确认只补两条 membership 后再应用。

### 第 2 步：启动 Gateway 并由用户验收

1. 备份 `~/.fastclaw-python/fastclaw.db` 及 WAL/SHM；
2. 使用合并后的 main 或当前 #19 head 在 18954 启动；
3. 确认 `/healthz`、`/readyz` 为 200；
4. 用原管理员登录，确认 27 Agent、892 Session、4 Team、25 Team member 可见；
5. 新建一个 Custom 测试团队和一个模板团队；
6. 运行 coordinator → specialist 委派；
7. 验证 Stop、归档、恢复、revision conflict；
8. 永久删除只在 disposable DB 或明确的测试团队上执行。

### 第 3 步：修复产品反馈

优先处理用户真实测试发现的 P0/P1；随后修 GitHub Issue #16 的标题清洗。不要在此阶段扩大到
channels/MCP 等新子系统。

### 第 4 步：真实 Provider 与业务切换门禁

1. DeepSeek 正常流、取消、429、5xx、EOF/cache token 证据；
2. 生产金融 coordinator 固定任务；
3. 世界杯 coordinator + 6 specialist + ledger report；
4. benchmark finance/runtime coordinator 固定任务；
5. 无悬挂 task、重复 completion、跨租户访问或非法 ToolCall 历史；
6. Go 18953 / Python 18954 使用独立数据库完成最终 differential。

### 第 5 步：首次切换

停止 Go、重新备份并重做 DB/asset dry-run 和导入，在 Python 18954 smoke 全绿后切换至 18953。
Go 二进制与源数据库继续保留。回滚只停止 Python 并恢复 Go 端口，不进行 Python → Go 数据迁移。

### 第 6 步：切换后的完整能力

按实际使用优先级迁移 channels/webhook → cron → MCP → sandbox → object store → usage/quota →
daemon/CLI。每项都必须有 contract、故障测试、API/Web E2E 和 parity matrix 后才算完成。

---

## 11. 安全与操作禁令

1. 不在命令、文档、fixture、日志或 PR 中写 Provider/API key、密码、cookie；
2. 不让 Python 指向 Go 的 SQLite、workspace 或 ledger 进行写操作；
3. 不删除或降级当前 live DB 的 Alembic revision；
4. 不用 `immutable=1` 读取可能带 WAL 的源库；
5. 不 rebase、不 force push 现有堆叠 PR；
6. 不在 Runtime 请求过程中自动安装 Skill 依赖；
7. 不把模型提供的身份字段当作可信上下文；
8. 不在 live 数据库上测试永久删除或 destructive migration；
9. 不宣称真实业务切换完成，直到第 10 节第 4 步有新的脱敏证据。

---

## 12. 文档索引

| 文档 | 用途 |
|---|---|
| `docs/migration/phase-1-provider-contracts.md` | Provider 与 streaming 契约 |
| `docs/migration/phase-2-data-identity.md` | Go DB、身份和导入规则 |
| `docs/migration/phase-3-single-agent.md` | AgentRunner、Session、Tool |
| `docs/migration/phase-4-multi-agent.md` | MessageBus/TaskQueue |
| `docs/migration/phase-5-runtime-manager.md` | Runtime manager 与配置策略 |
| `docs/migration/phase-6-assets-skills.md` | 资产和 Skill 环境 |
| `docs/migration/phase-7-gateway-web.md` | Gateway/Web 兼容 API |
| `docs/migration/phase-8-plugins-finance.md` | Plugin 与 finance-tools |
| `docs/migration/phase-9-release-cutover.md` | CI、Docker、differential |
| `docs/migration/phase-10-cutover-readiness.md` | 历史切换计划；部分运行状态已过期 |
| `docs/migration/evidence/` | 已留存的脱敏 smoke/differential 证据 |
| `tests/fixtures/go792/README.md` | 锁定 Go fixture 的生成与校验 |
| `tests/fixtures/web-python-overlays.json` | Web overlay 来源、原因和 SHA-256 |
| `REVIEW-fastclaw-python-stacked-prs-2026-08-04.md` | 原始 F1–F12 审查背景，引用时核对已撤销条目 |
