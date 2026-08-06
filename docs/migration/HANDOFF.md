# FastClaw Python 交接手册

- 交接日期：2026-08-06
- 目标仓库：`https://github.com/M0yuW/fastclaw-python`
- 行为参考（只读，不逐行翻译）：`https://github.com/M0yuW/fastclaw` @ `792417b86b5c12af1b99364865217a74f4d52f38`
- 本手册用途：任何接手人只读本文即可判断「现在在哪、下一步做什么、哪些事不能做」。

阅读顺序建议：第 1 节（怎么恢复工作现场）→ 第 3 节（阶段与 PR 全景）→ 第 5 节（现在就能干的活）→ 第 8 节（锁定约束，动手前必读）。

---

## 1. 恢复工作现场

```bash
cd /Users/wangzheyu/.codex/worktrees/8e65/fastclaw/fastclaw-python
git status                 # 提交本轮 G 阶段后应为干净
git log --oneline -1       # 应为当前 release-hardening 分支最新提交
```

| 项 | 值 |
|---|---|
| 工作树路径 | `/Users/wangzheyu/.codex/worktrees/8e65/fastclaw/fastclaw-python` |
| 当前分支 | `codex/release-hardening` |
| HEAD | `4c9bc98` 加本轮 G 阶段收口提交 |
| `origin/main` | `4c284d1` |
| 未推送提交数 | 以 `git rev-list --count origin/main..HEAD` 为准 |
| upstream | **未配置**——从未 push、从未 rebase、从未 force push |
| 工作树 | G 阶段提交后应干净 |

`origin/main..HEAD` 至少包含以下 7 个功能提交，另加 G 阶段收口提交（新→旧）：

```
4c9bc98  Restore Go agent and tool event contracts
bdebde3  Harden release and differential cutover gates
4a75793  Add supervised finance plugin runtime
3b2442e  Stabilize Playwright history assertion
6aa21c0  Complete Gateway and Web compatibility APIs
d07700c  Import assets and prepare production skills
09156de  Wire application Agent runtime manager
```

---

## 2. 分支拓扑与 PR 状态

严格祖先链（每个分支都是下一个的祖先，无交叉、无 rebase）：

```
origin/main (4c284d1)
  └─ codex/runtime-manager      (09156de)  PR #9   OPEN，已推送
      └─ codex/assets-skills    (d07700c)  PR #10  OPEN，已推送
          └─ codex/gateway-web-api (3b2442e) PR #11 OPEN，已推送，5/5 CI 绿
              └─ codex/plugin-finance-tools (4a75793)  无 remote，无 PR
                  └─ codex/release-hardening (4c9bc98)  无 remote，无 PR ← 当前 HEAD
```

| PR | 分支 | 主题 | 状态 |
|---|---|---|---|
| #1 | — | Initialize repository | MERGED |
| #2 | — | Provider 契约 | MERGED |
| #3 | — | 数据/身份/Go 导入 | MERGED |
| #4 | — | 单 Agent 运行时 | MERGED |
| #5 | — | 多 Agent 运行时 | MERGED |
| #6 | — | Next.js Web 快照 | MERGED |
| #7 | — | Alembic 基线 | MERGED |
| #8 | — | Gateway 鉴权 + Provider API | MERGED |
| #9 | `codex/runtime-manager` | AgentRuntimeManager 接入 lifespan | **OPEN**，base = `main` |
| #10 | `codex/assets-skills` | 资产导入与生产 skills 准备 | **OPEN**，base = `codex/runtime-manager` |
| #11 | `codex/gateway-web-api` | Phase D Gateway/Web 兼容 API | **OPEN**，base = `codex/assets-skills` |
| 待建 | `codex/plugin-finance-tools` | Phase E 插件协议 + finance tools | **未推送**（额度阻断） |
| 待建 | `codex/release-hardening` | Phase F 发布加固 + differential | **未推送**（额度阻断） |

**合并规则（不可违反）**：逐个 retarget 到 `main`，**不 rebase、不 force push**。原因是保住 PR 行内评论的锚点；一旦 rebase，历史评论全部失去位置，审查上下文不可恢复。父 PR 合并后子 PR 只做 base 切换 + 重跑 CI。

合并顺序：`#9 → #10 → #11 → `（推送后的 Phase E PR）`→ `（推送后的 Phase F PR）。

---

## 3. 阶段 → 功能目标 → PR 映射

每阶段有一份设计文档在 `docs/migration/`，是该阶段的权威口径；本表只做索引。

| 阶段 | 功能目标 | 文档 | 承载 PR | 状态 |
|---|---|---|---|---|
| 1 | Provider 契约：Anthropic / OpenAI 兼容 SSE，delta 累加，`_raw` 回放保签名与 prompt cache | `phase-1-provider-contracts.md` | #2 | 已合并 |
| 2 | 数据与身份：SQLAlchemy 2 async schema、bcrypt/API key/ACL、Go SQLite 单向导入 | `phase-2-data-identity.md` | #3 | 已合并 |
| 3 | 单 Agent 运行时：AgentRunner、内建工具（exec/read/write/webfetch）、会话续聊 | `phase-3-single-agent.md` | #4 | 已合并 |
| 4 | 多 Agent 运行时：MessageBus、任务队列、委派去重、等待图环检测、取消语义 | `phase-4-multi-agent.md` | #5 | 已合并 |
| — | Web 快照：Next.js 16.1.6 / React 19.2.3 逐文件保真迁入 + overlay 清单 | — | #6 | 已合并 |
| — | Alembic 基线：冻结初版 revision，`alembic check` 漂移门禁替代 `create_all()` | `alembic-schema-integrity.md` | #7 | 已合并 |
| — | Gateway 鉴权 + Provider API | — | #8 | 已合并 |
| 5 | AgentRuntimeManager：FastAPI lifespan 装载 27 个 Agent 到 MessageBus，子 Agent 继承父的可信 user/root/call-path，no-tools / delegate-only / custom 策略 | `phase-5-runtime-manager.md` | #9 | OPEN |
| 6 | 资产与 Skills：幂等 `fastclaw migrate import-assets`（dry-run / 冲突检测 / 逐文件 SHA-256 / 审计报告），按 requirements hash 建 per-skill venv | `phase-6-assets-skills.md` | #10 | OPEN |
| 7 | Gateway / Web 兼容 API：Web 实际调用的全部端点，结构化 `unsupported` 取代模糊 404，浏览器 Abort → Gateway → Agent → Tool/Provider 的停止链 | `phase-7-gateway-web.md` | #11 | OPEN |
| 8 | 插件协议与 finance tools：JSON-RPC 进程生命周期、握手、`tool.list`/`tool.execute`、超时重启、`_TRUSTED_ARGUMENT_NAMES` 身份拒绝；租户隔离的 thesis ledger / watchlist / 事件指纹 / 乐观版本 | `phase-8-plugins-finance.md` | 待建（`codex/plugin-finance-tools`） | 本地完成，未推送 |
| 9 | 发布加固与切换：PostgreSQL/wheel/Docker/Playwright/依赖审计/secret scan CI，Go 18953 vs Python 18954 differential，故障矩阵，结构化日志 | `phase-9-release-cutover.md` | 待建（`codex/release-hardening`） | 本地完成，未推送 |
| 10 | 切换就绪与遗留收口：G 口径收口 / H 凭据轮换 / I 合并堆叠 / J 真实环境验证 / K 切换执行 | `phase-10-cutover-readiness.md` | 当前分支 | G 已实现并验证；H–K 待外部条件 |

代码规模：`src/fastclaw/` 54 个 Python 文件，`tests/` 24 个 Python 测试文件，finance plugin 另有 1 个来源契约测试文件。模块划分：`agent/`、`gateway/`、`migration/`、`orchestration/`、`plugin/`、`providers/`、`storage/`、`tools/`，以及顶层 `app.py`、`cli.py`、`differential.py`、`execution.py`、`identity.py`、`models.py`、`observability.py`、`runtime.py`、`skills.py`。

---

## 4. 已验证基线（2026-08-06，在本轮 G 阶段改动上独立复跑）

接手后第一件事是复跑下表，确认现场未被外部改动。数字应逐字一致；不一致说明有人动过代码或环境。

| 命令 | 预期输出 |
|---|---|
| `pytest -q` | `120 passed, 1 skipped`（skip = 本机无 PostgreSQL 服务） |
| `(cd plugins/finance-tools && ../../.venv/bin/python -m unittest -v test_plugin.py)` | `10 passed` |
| `ruff check .` | `All checks passed` |
| `ruff format --check .` | `104 files already formatted` |
| `mypy` | `Success: no issues found in 78 source files`（strict） |
| `alembic upgrade head` && `alembic check` | 升级至 `20260805_01`，无漂移 |
| `python scripts/verify_web_snapshot.py` | `86 unchanged + 4 declared overlays + 4 attributed additions` |
| `pnpm --dir web lint` / `build` | exit 0；build 产出 25 routes |

注意：`alembic upgrade head` 会在工作树里生成 `fastclaw.db`，复跑后请删除，否则 `git status` 不干净。

Provider 凭据核对命令应报告 DeepSeek / OpenRouter / ODDS **三项均未配置**——这是当前的正确状态，不是缺陷（见第 5.2 节）。

CI（`.github/workflows/ci.yml`）的 job：`quality`（Python 3.12/3.13/3.14 + `alembic check`）、`web`（+ 快照校验）、`postgres`（postgres:17-alpine，`pytest -q -m postgres`）、`package`、`docker`、`security`（pip-audit + gitleaks）、`web-e2e`（Playwright，依赖其余全部）。

`.github/workflows/differential.yml` 目前只有 `workflow_dispatch` + `runs-on: self-hosted`，**从未对真实双服务运行过**——见 5.2 J1。

---

## 5. 下一步做什么

完整计划见 `phase-10-cutover-readiness.md`；本节只给「现在能做 / 现在不能做」的裁决。

### 5.1 阶段 G——口径与盲区收口

本阶段代码与文档变更已实现，完成验证后即可解除对应合并阻断：

- **G1 Web 快照口径**统一为 `86 unchanged + 4 declared overlays + 4 attributed additions`，并由脚本输出和机械搜索共同锁定。
- **G2 Web overlay 哈希**已写入 `web-python-overlays.json`；`verify_web_snapshot.py` 会校验全部 4 个 overlay，参数化测试逐一证明篡改会失败。
- **G3 可信身份契约**已由回归测试锁定：`spawn_subagent` schema 只能暴露 `agent_id` 与 `task`，模型提供的 user/root/call-path 字段不影响可信上下文；plugin 的 10 个 camelCase/snake_case 受保护名称均逐一拒绝。
- **G4 differential 状态**已纠正：`tests/test_differential.py` 通过 `httpx.MockTransport` 覆盖比对逻辑，真实 fixture 只由运行脚本加载；Go/Python 双服务尚未实际执行，真实运行与报告仍是切换硬前置。

**阶段 H——凭据轮换**：交接文档中出现过的全部口令、DeepSeek key、OpenRouter key、ODDS key 全部轮换。这些凭据在 Phase B–E 全部开发过程中一直有效，暴露窗口随时间线性增长；既然确定要轮换，现在轮换严格优于切换时轮换。新值只经环境变量或 secret 注入，轮换记录只记「已轮换 + 时间 + 责任人」。

G 与 H 可并行，互不依赖。

### 5.2 当前被阻断

阻断原因有两类：**外部操作额度**（无法 push、无法重启真实 18954 服务）与**集中凭据缺失**（DeepSeek / OpenRouter / ODDS）。

| 项 | 内容 | 缺什么 |
|---|---|---|
| 推送 Phase E/F | `codex/plugin-finance-tools`、`codex/release-hardening` 推送并建 Draft PR | 外部操作额度 |
| J1 | differential 真实双服务运行（Go 18953 / Python 18954，**各自独立数据根，绝不共享 SQLite 文件**） | 宿主端口权限 |
| J2 | 真实 provider 异常语义：正常流 / 主动中断 / 429 / 5xx；重点核对 `cache_read_tokens`、`cache_write_tokens` 上报时机与累加值，以及 EOF 与畸形 SSE 是否被误判为成功。当前这些只在 MockTransport 下验过 | DeepSeek / OpenRouter 凭据 |
| J3 | finance / World Cup / benchmark 三套固定 fixture 端到端 smoke，断言无悬挂 task、无重复 completion、无跨租户访问、无失配 ToolCall 历史 | 三项凭据 + 端口权限 |

阻断解除前**不得进入阶段 K（切换执行）**。

### 5.3 依赖关系

```
G（口径与盲区收口）──┐
                     ├─→ I（合并堆叠）──→ J（真实环境验证）──→ K（切换执行）
H（凭据轮换）────────┘                        ▲
                                              └── 当前被外部额度阻断
```

I 依赖 G——否则把错误口径合入 `main`。

---

## 6. 已接受的行为差异

以下不是缺陷，是迁移中被显式接受的差异。**必须让运维知道**，否则上线后会被当 bug 报回。

| 项 | 差异 | 理由 |
|---|---|---|
| LEO Agent | 固定使用系统默认模型，与 Go 行为不同 | 源数据缺 `agent.json` / `SOUL.md`，无配置可继承 |
| Channel `credential_key` | 导入时置空，入站路由中断，需重新配置 | 凭据安全优先；bot token 同时被清，channel 本就要重配。这是**功能性中断**，报告中单列 warning，不计入脱敏计数 |
| 会话数据流向 | 单向 Go → Python，Python 不回写 Go | Python 数据库格式为权威格式 |
| Skill 依赖 | 运行时禁止自动安装，需显式 `fastclaw skills prepare` | 避免运行时网络与隐式环境变更 |
| 未知端点 | 返回结构化 `unsupported`，不是模糊 404 | 让 Web 能区分「没实现」与「路径错」 |

---

## 7. 未关闭的技术项

### 7.1 WebFetchTool DNS rebinding（低优先，**不得记为已关闭**）

`src/fastclaw/tools/builtin.py:336-361` 解析主机名并检查全部返回地址 `is_global`，随后 `self._client.stream("GET", url, ...)` 用**主机名**再发一次请求——校验与连接是两次独立 DNS 查询。攻击者控制权威 DNS、返回短 TTL、第一次给公网 IP、第二次给 `169.254.169.254`，即可绕过。

当前实现已挡住绝大多数现实攻击（直接给内网 IP、`localhost`、解析到内网的域名），rebinding 需要攻击者控制权威 DNS，故定低优先。修法：把校验通过的 IP 钉到连接上——连 IP、`Host` 头带原主机名、HTTPS 下 SNI 显式设为主机名；或用自定义 transport / resolver 钩子复用首次解析结果。

状态：backlog。**任何报告都不得把此项标为已修复。**

### 7.2 Phase B 门禁需加强

原路线的「benchmark coordinator 能通过 Mock Provider 调用指定 specialist」只覆盖 happy path。真正该守的是身份不可伪造，已在 G3 中补齐；本条在 G3 完成后关闭。

### 7.3 历史审查报告中的两条误判（已撤销，勿再引用）

- 「`_handlers[agent_id]` 跨租户静默覆盖」**是误判**。`orchestration/bus.py` 的 `register()` 有 `if agent_id in self._handlers: raise ValueError(...)`；且 Go 侧 `generateID("agt_")` 使 Agent ID 全局随机唯一，碰撞不可能。仅去重键补了 tenant 维度。
- 「`batch()` 缺 `return_exceptions` 导致异常无人取回」中「异常无人取回」部分**未能复现**——CPython 的 `_done_callback` 会取回迟到的兄弟异常。成立的部分是提前退出、兄弟任务继续运行且结果被丢弃，已按批处理生命周期缺陷修复。
- 另有一条「CI Python 3.14 缺 wheel」的疑虑已被证伪并撤销：`uv pip install -e ".[dev]"` 在 3.14 下退出码 0。

---

## 8. 锁定约束（动手前必读）

以下为已锁定的决策，**接手人不得在没有显式授权的情况下更改**。每条都有已实现的代码或测试支撑，改动会连带破坏一批断言。

**数据与迁移**

1. Python 数据库格式为**权威格式**。回写 Go 不受支持。只要求 Go blob 能无损导入并在 Python 侧回放。
2. **凭据安全优先于开箱即用**：所有秘密及 `credential_key` 在导入时清空，切换前重新配置。
3. **孤儿数据默认阻断**；只有显式 `quarantine` 才允许继续，且报告必须完成源行、活动行、隔离行三方对账。
4. 脱敏报告**永不包含原值**；只输出被脱敏的字段路径、config ID 与实际计数。
5. 源库以 `mode=ro` 打开，SHA-256 钉定；源校验在目标事务 **commit 之前**完成。

**身份与权限**

6. `user_id` / `agent_id` / `session_id` / `root_execution_id` **只能由 Runtime 注入**；同名模型参数被忽略或拒绝（`plugin/manager.py` 的 `_TRUSTED_ARGUMENT_NAMES`）。
7. **模型参数不得扩大工具权限**。no-tools / delegate-only / custom 策略由服务端决定。
8. 子 Agent 继承父的可信 user / root / call-path，不接受模型提供的身份。
9. Provider live ToolCall ID 在来源不可信时 fail-closed；**RawAssistant 不被改写**（`providers/stream.py` 按 `_raw` 来源分流：本地构造路径先归一化再造 `_raw`，provider 权威路径只校验不改写）。

**运行时与安全**

10. Skill 依赖**运行时禁止自动安装**。
11. Python **不读取也不写入** Go 的 workspace / ledger。
12. 敏感 provider 数据、绝对路径、SQL **不得进入模型可见的错误**（`bus.py:207-228` 的 `_safe_error` 按异常类型映射固定文案，未知异常只带 correlation_id）。
13. `cancel_root` 是**显式 API**，不是任一等待者取消时的副作用；全仓仅 `agent/manager.py:221`（Stop 路径）调用。
14. Docker 以非 root 只读运行（`USER 10001:10001`）。

**流程**

15. **不 rebase、不 force push、不改写历史**——保住 PR 行内评论锚点。
16. differential 双服务**永远使用独立数据库**，绝不共享 SQLite 文件。
17. 秘密只经环境变量或 secret 注入，**不写入任何文档、fixture、报告或数据库**。

---

## 9. 切换与回滚

前置：G、H、I、J 全部完成。详细步骤见 `phase-10-cutover-readiness.md` 第 6 节。

1. 停止 Go，重新备份最新 DB / WAL / SHM、workspace、skills。
2. 对新版本目录先跑 DB 与 asset 的 dry-run，核对报告后再正式导入。
3. 配置 DeepSeek / OpenRouter / ODDS（轮换后的新值）。
4. 在 18954 复跑三套固定 fixture smoke，用真实迁移数据而非安全副本。
5. 确认无悬挂 task、无重复 completion、无跨租户访问、无非法 ToolCall 历史。
6. Python 切到 18953；Go 二进制、源库与备份继续保留。

**回滚**：仅停止 Python 并恢复 Go 18953。**Python 数据永不回写 Go**，只作审计备份留存。回滚路径应在 J 阶段用安全副本演练一次。

验收：切换后首个完整工作日内无 P0/P1 事故。

---

## 10. 相关文档索引

| 文档 | 内容 |
|---|---|
| `docs/migration/phase-1`…`phase-9` | 各阶段设计与验收口径，该阶段的权威来源 |
| `docs/migration/phase-10-cutover-readiness.md` | 阶段 G–K 的完整后续计划与依赖图 |
| `docs/migration/alembic-schema-integrity.md` | 冻结 revision 与漂移门禁机制 |
| `tests/fixtures/go792/README.md` | Go fixture 的再生成方法；bcrypt salt 与 SQLite 页布局不确定，故校验比对逻辑内容而非字节 |
| `tests/fixtures/web-python-overlays.json` | Web overlay 清单（`referenceCommit` = `792417b…`） |
| 主仓 `REVIEW-fastclaw-python-stacked-prs-2026-08-04.md` | 原始堆叠 PR 审查报告（F1–F12），含已撤销条目，引用时对照本手册 7.3 节 |
