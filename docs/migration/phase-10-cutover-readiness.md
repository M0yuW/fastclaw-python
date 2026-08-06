# 后续开发计划：切换就绪与遗留收口

- 制定日期：2026-08-06
- 当前分支：`codex/completion-audit`（从已合并的 `main@4f1df92` 创建）
- 当前基线：PR #1–#14 已合并；Python 18954 正在运行
- 参考 Go 实现：`792417b86b5c12af1b99364865217a74f4d52f38`（只读）

本文档接续 `phase-1` 至 `phase-9`。前九阶段和合并工作已经完成；当前主题是把
「实现完成」推进到「可切换」，并收口真实 Provider、认证/SSE differential 与
三套生产 smoke。

---

## 1. 已验证基线

以下为 2026-08-06 在本轮 G 阶段改动上独立复跑的结果，作为后续工作的事实起点。

| 项目 | 结果 |
|---|---|
| `pytest -q` | 142 passed, 1 skipped（PostgreSQL 本机无服务） |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 112 files already formatted |
| `mypy` | Success: no issues found in 83 source files |
| `alembic upgrade head` + `alembic check` | 升级至 `20260805_01`，无漂移 |
| `scripts/verify_web_snapshot.py` | 86 unchanged + 4 declared overlays + 4 attributed additions |
| 分支状态 | 从 `main@4f1df92` 追加提交，未 rebase，未 force push |

抽查确认到位的修复：

- `providers/stream.py:84-97` 按 `_raw` 来源分流——本地构造路径先归一化 ToolCall ID 再造 `_raw`，provider 权威路径只校验不改写。空 ID 未被一律 fail closed。
- `providers/models.py:123-141` 时间戳按来源判别（`int|float` → 毫秒，`str` → RFC3339），不依赖数量级猜测。
- `tests/fixtures/go792/generator/main.go` 确实 import `internal/provider` / `internal/session` / `internal/store` / `bcrypt`，fixture 由锁定 Go commit 真实生成。
- `orchestration/queue.py` 有 `JobState.CANCELLING`，去重仅命中 `{QUEUED, RUNNING}`，waiter 释放与状态转换在同一把锁内完成。
- `tools/builtin.py` exec 三项齐备：`start_new_session=True` + `killpg` 分级终止 + `st_dev`/`st_ino` 校验 + 增量读取截断。
- `cancel_root` 已降为显式 API，全仓仅 `agent/manager.py:221`（Stop 路径）调用。
- `bus.py:207-228` `_safe_error` 按异常类型映射固定文案，未知异常只带 correlation_id。
- 实测：显式 `cancel_root` → batch 返回 `CANCELLED` outcome；外层取消 batch → `CancelledError` 正确传播，未被 `return_exceptions` 吞掉。

已可关闭的原审查遗留风险：

- **`chat()` / `stream()` 等价性**：两个 provider 的 `chat()` 均为 `stream()` 耗尽后取 `stream.result()`（`openai.py:58-62`、`anthropic.py:63-67`），共用同一 `ResponseAccumulator`，结构上不可能不等价，同时排除单轮重复请求。从设计上关闭，无需额外测试。
- **bcrypt / API key / ACL 跨语言兼容**：`tests/test_importer.py:253-257` 已用 Go fixture 生成的 hash 实跑 Python 侧 `verify_password`、`hash_api_key` 与 `api_key_can_access_agent`。已验证，关闭。
- **`spawn_subagent` 身份伪造面**：`orchestration/tool.py` 的参数 schema 仅 `agent_id` 与 `task`，身份全部来自 `ExecutionContext`，模型无字段可影响 `user_id`。结构上关闭，仅缺锁定契约的回归测试（见 G3）。

---

## 2. 阶段 G：口径与盲区收口（已实现并验证）

本阶段全部为小改动，但都属于「测试在看却看不见」一类，与 F1 同源，必须在推送前完成。

### G1 · 统一 Web 快照验收口径

此前交接手册与 PR 描述没有区分未改动文件和已声明 overlay，与 `4c9bc98` 的实际组成不一致。实测 HEAD：

```
只在 Python:  LICENSE  SOURCE.md  e2e/runtime.spec.ts  playwright.config.ts
只在 Go:      next-env.d.ts
共有:         90（其中 4 个为已声明 overlay）
```

- 把所有文档、PR 描述、交接手册的验收口径改为脚本实际执行的三个数字：**86 个未改动 + 4 个已声明 overlay + 4 个归属新增**。
- 口径必须与 `scripts/verify_web_snapshot.py` 的输出逐字对应，可机械核对。

**验收**：全仓快照验收摘要统一为 `86 unchanged + 4 declared overlays + 4 attributed additions`；`grep` 可机械核对。

### G2 · 给 Web overlay 钉哈希

`verify_web_snapshot.py:97-103` 跳过 `modified` 集合中的文件，因此 4 个 overlay 此后无任何内容保护。其中 `src/lib/api.ts` 承载只读 actAs 传播，`src/app/agents/page.tsx` 承载管理员导航——权限相关代码可被静默改动而 CI 全绿。

已实测确认该行为：改 `src/app/page.tsx`（未声明）→ `RuntimeError: web snapshot hashes differ`；改 `src/lib/api.ts`（已声明 overlay）→ 通过。

- `web-python-overlays.json` 的 `modifiedFiles` 从 `{path: reason}` 扩展为 `{path: {reason, sha256}}`。
- 校验时对 overlay 文件比对声明的 SHA-256；改动 overlay 必须同步更新清单，形成显式审计动作。
- 新增单测：篡改任一 overlay 文件而不更新清单，校验必须失败。

**验收**：对 4 个 overlay 逐一注入改动，校验全部报错。

### G3 · 补齐可信身份的锁定契约测试

`spawn_subagent` 当前靠「schema 里没有身份字段」隐式安全。plugin 侧有显式的 `_TRUSTED_ARGUMENT_NAMES` 拒绝逻辑，委派侧没有等价物——将来给 schema 加字段时没有任何测试会拦住。

- 新增测试：向 `SpawnSubagentTool.execute` 传入 `user_id` / `userId` / `root_execution_id` / `call_path` 等字段，断言这些字段不影响实际执行身份（即被忽略或被拒绝）。
- 新增测试：断言 `spawn_subagent` 的 `parameters.properties` 键集合恰好为 `{agent_id, task}`，schema 扩张即失败。
- 同一模式补一条 plugin 侧断言，锁定 `_TRUSTED_ARGUMENT_NAMES` 覆盖 camelCase 与 snake_case 两套写法。

**验收**：以上测试在移除对应防护后必须失败（人工反向验证一次）。

### G4 · 修正 Phase E/F 的完成度表述

`docs/migration/phase-9-release-cutover.md` 已区分 harness 单测和真实运行。2026-08-06
已完成独立 Go/Python 的未认证 health/status 子集并留证；认证/SSE/工具路径仍未运行，
不得把基础探针报告扩张解释为完整 parity。

- 在 phase-9 中区分「比对逻辑已实现并有单测覆盖」与「尚未对真实双服务执行」。
- 把 differential 真实运行列为切换的**硬前置**，不是可选项。

**验收**：phase-9 中出现明确的「未对真实服务执行」状态标注。

---

## 3. 阶段 H：凭据轮换（前置于一切真实环境工作）

**这一步从 phase-9 的第 1 步提前到此处。** 需要轮换的凭据在 Phase B–E 的全部本地开发过程中一直有效，暴露窗口随时间线性增长；既然已确定要轮换，现在轮换严格优于切换时轮换，且不影响任何后续阶段（后续本来就要重新配置）。

- 轮换范围：交接手册中出现过的全部口令、DeepSeek key、OpenRouter key、ODDS key。
- 轮换后旧值立即失效，不保留「过渡期双活」。
- 新值只经环境变量或 secret 注入，不写入任何文档、fixture、报告或数据库。
- 轮换记录只记「已轮换 + 时间 + 责任人」，不记值本身。

**验收**：`fastclaw` provider 核对命令对三项凭据报告「未配置」（当前状态即如此），配置新值后报告「已配置」；仓库 secret scan 通过。

---

## 4. 阶段 I：合并现有堆叠（已完成）

PR #1–#14 已按 `#9 → #10 → #11 → #12 → #13 → #14` 全部合并。每一级均把最新
`main` 以 merge commit 向前合入、retarget 到 `main` 并重跑 CI；全程没有 rebase
或 force push。当前 `main` 为 `4f1df92`。

- 每次父 PR 合并后，子 PR 均已 retarget 并重跑 Python 3.12/3.13/3.14、Web、
  Alembic、快照及该分支可用的后续门禁。
- 最终 #13 在 `main` 基线上通过 PostgreSQL、package、Docker、security 与
  Playwright 在内的 9/9 CI。

**验收**：已完成，`main@4f1df92` 工作树可干净检出。

---

## 5. 阶段 J：真实环境验证（进行中，凭据阻断）

先对一次性安全副本运行 `fastclaw cutover audit`。该命令固定核对 2 用户、
M0yuW 13 Agent、benchmark 14 Agent、26 个角色文件 profile、模型来源、benchmark
工具策略、Skill 环境、Provider、ODDS、plugin、数据库 FK、Session 与 channel
credential 清空状态；任何 blocker 都返回退出码 2。审计会启动 bundled plugin
完成握手，因此禁止直接指向 Go 或线上 Python 数据根。

宿主端口权限已验证可用。集中凭据仍缺失，阻断解除前不得进入阶段 K。

### J1 · differential 真实双服务运行

- Go 18953 与 Python 18954 各自独立数据根，绝不共享 SQLite 文件。
- 对比范围沿用已实现的 harness：状态码、递归 JSON shape、SSE v2 事件顺序与字段、单调 sequence、终态 `done`、ToolCall/ToolResult 配对。
- 报告作为 artifact 留存，作为切换决策的书面依据。

**验收**：差异报告为空，或每条差异都有书面接受理由。

2026-08-06 已完成第一轮真实运行：独立 Go 18953 与 Python 18954 的 health/status
共同语义通过，证据见 `evidence/differential-smoke-2026-08-06.json`。原
`/v1/agents` fixture 暴露 Go launcher 未传播 HTTP endpoint 配置的问题，锁定 Go
会把该路径交给 SPA，而不是 JSON API；默认 fixture 已改为真实可执行的共同端点。
本轮仅关闭未认证基础探针，认证 Agent/chat、SSE、Provider、工具与取消仍待凭据。

同日又在 disposable Python 副本的 18955 上，用只写入副本的一次性 Web session
完成认证 API smoke：管理员身份、2 用户、M0yuW 的 13 Agent、只读 actAs 下的
benchmark 14 Agent，以及 coordinator 的 5 个 Session 均通过。该验证不使用旧密码
或 API key，证据见 `evidence/authenticated-api-smoke-2026-08-06.json`。因此认证读取
链已关闭。随后在同一隔离策略下以确定性 Provider 跑通认证 chat/SSE、工具和取消；
真实双服务认证差分与真实 Provider 行为仍待轮换后凭据。

### J2 · 真实 provider 异常语义

当前 usage / cache token 累加、EOF、畸形 SSE、429/5xx 全部只在 MockTransport 下验过。Phase F 的「Provider EOF/429/5xx 故障矩阵」若同为 mock，则本项状态不变。

- 用真实 DeepSeek / OpenRouter 端点跑一轮：正常流、主动中断、限流、5xx。
- 重点核对 `cache_read_tokens` / `cache_write_tokens` 的上报时机与累加值，以及 EOF 与畸形 SSE 是否被误判为成功。

**验收**：四类异常各有一条真实响应的记录，且行为与 mock 下一致或差异已被记录。

### J3 · 三套固定 fixture 的端到端 smoke

- finance、World Cup、benchmark 各跑一次固定 fixture，经 Runtime 调用插件完成一次状态读写。
- 断言无悬挂 task、无重复 completion、无跨租户访问、无失配 ToolCall 历史。

**验收**：三套 smoke 全绿且上述四项断言成立。

2026-08-06 已先完成不依赖生产凭据的 Runtime wiring smoke。它使用真实迁移 profile
和 disposable 数据副本，通过本地确定性 OpenAI-compatible SSE Provider 运行：生产
金融 coordinator、世界杯 coordinator（6 个专家 + 账本 direct-return）、金融
benchmark coordinator、Runtime benchmark coordinator。18 个 ToolCall/ToolResult
全部配对，每条流恰好一个 done，跨租户 Session 为 0，结束后活动任务为 0；另以慢流
验证 HTTP Abort 后 Provider/任务取消且残缺 assistant 不落库。证据见
`evidence/runtime-wiring-smoke-2026-08-06.json`。

这关闭的是 Runtime/Gateway 接线门禁，不替代真实 DeepSeek/OpenRouter/ODDS 的业务
结果验收；真实三套 smoke 仍是阶段 K 前置。

复现入口为 `scripts/fixture_multiagent_provider.py` 与
`scripts/cutover_wiring_smoke.py`。后者会写入测试 Session，必须显式传入
`--acknowledge-disposable-copy`，且硬拒绝默认 Go/Python live 数据库路径；两个脚本均
被 sdist 完整性门禁锁定。session cookie 只应使用写入副本的一次性值，不得使用或
记录旧账号口令/API key。

---

## 6. 阶段 K：切换执行

前置：G、H、I、J 全部完成。

1. 停止 Go，重新备份最新 DB / WAL / SHM、workspace、skills。
2. 对新版本目录先跑 DB 与 asset 的 dry-run，核对报告后再正式导入。
3. 在 18954 复跑三套固定 fixture smoke（J3 的重跑，用真实迁移数据而非安全副本）。
4. 确认无悬挂 task、重复 completion、跨租户访问或非法 ToolCall 历史。
5. Python 切到 18953；Go 二进制、源库与备份继续保留。

回滚：仅停止 Python 并恢复 Go 18953。**Python 数据永不回写 Go**，只作审计备份留存。

**验收**：切换后首个完整工作日内无 P0/P1 事故；回滚路径经过一次演练（可在 J 阶段用安全副本演练）。

---

## 7. 已接受的行为差异

以下不是缺陷，是迁移中被显式接受的差异。必须写入交接手册，否则上线后会被当 bug 报回。

| 项 | 差异 | 理由 |
|---|---|---|
| LEO Agent | 固定使用系统默认模型，与 Go 行为不同 | 源数据缺 `agent.json` / `SOUL.md`，无配置可继承 |
| Channel `credential_key` | 导入时置空，入站路由中断，需重新配置 | 凭据安全优先；bot token 同时被清，channel 本就要重配。这是**功能性中断**，报告中单列 warning，不计入脱敏计数 |
| 会话数据流向 | 单向：Go → Python。Python 不回写 Go | Python 数据库格式为权威格式 |
| Skill 依赖 | 运行时禁止自动安装，需显式 `fastclaw skills prepare` | 避免运行时网络与隐式环境变更 |

---

## 8. 技术项收口

### 8.1 WebFetchTool DNS rebinding（已关闭）

旧实现先解析并检查全部地址，再让 HTTPX 按主机名做第二次 DNS 查询，存在
DNS rebinding 窗口。现已为 WebFetch 配置独立的、禁用环境代理的 HTTP client：

- 每一跳先解析，任一地址非公网即拒绝；
- 已验证的地址通过 `ContextVar` 绑定到当前请求；
- 自定义 httpcore network backend 只向这些 IP 建立 TCP 连接，不再次解析域名；
- 原主机名继续用于 HTTP `Host`、TLS SNI 与证书校验；
- 无 pin 的连接和 Unix socket fail closed，HTTP/2 与环境代理关闭；
- Provider 继续使用原共享 client，不受 WebFetch 安全 transport 影响。

测试锁定 TCP 目标、多个公网 IP fallback、无 pin 拒绝、Host/SNI 保真、私网和逐跳
重定向拒绝。状态：**已关闭**。

### 8.2 Phase B 门禁需加强

原路线的「benchmark coordinator 能通过 Mock Provider 调用指定 specialist」只覆盖 happy path。真正该守的是身份不可伪造，已在 G3 中补齐；本条在 G3 完成后关闭。

### 8.3 启动失败与 shutdown 清理（已关闭）

Lifespan 现用 `AsyncExitStack` 在启动阶段逐项注册逆序清理；Agent manager 尚未完成
启动就失败时，也会尝试停止 bus/plugin、关闭 Provider/WebFetch 两个 HTTP client
并释放数据库。manager shutdown 会收集单项清理异常，继续关闭其余资源后统一报告。

### 8.4 同轮多 Agent 委派（已关闭）

底层 `MessageBus.batch()` 原本已并发、保序并脱敏错误，但 `AgentRunner` 未调用它，
所以模型同轮发出的多个委派仍会串行。现新增显式 `BatchTool` 协议，仅安全声明该能力
的工具可批量执行；`spawn_subagent` 将请求交给 `MessageBus.batch()`。不同目标并行、
同目标仍受队列 FIFO 约束，结果按 ToolCall 输入顺序写回。文件、exec、账本等普通工具
继续串行，避免隐式扩大副作用。

### 8.5 Provider 核对命令的只读语义（已关闭）

凭据核对只需要 Agent profile、Provider 选择和 Skill 状态，旧实现仍会启动 finance
plugin，从而执行状态库 migration。`fastclaw providers check` 现以
`enable_plugins=False` 加载 manager，plugin 不启动、不写状态库；正式 Gateway 与
`fastclaw cutover audit` 保持 plugin 启用。回归测试锁定该差异。

---

## 9. 阶段依赖与阻断关系

```
G（口径与盲区收口）──┐
                     ├─→ I（合并堆叠）──→ J（真实环境验证）──→ K（切换执行）
H（凭据轮换）────────┘                        ▲
                                              └── 当前被凭据轮换/配置阻断
```

- G 与 I 已完成。
- Python 18954 正在运行；宿主端口权限已验证。
- J 的未认证基础探针已完成，剩余部分依赖三项轮换后的集中凭据。
- 当前阻断点：凭据轮换/配置尚未由责任人完成；阻断解除前 K 不可开始。
