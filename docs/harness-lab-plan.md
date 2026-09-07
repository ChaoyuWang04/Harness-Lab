# Harness Lab · 最小生产化 Serving 系统实施计划书

> 目标：以 Qwen3-0.6B（ollama）为模型 provider，从零搭起一套**最小但真实**的 Agent 生产化 Harness：
> PostgreSQL 单一事实源 + Outbox + Redis 队列（API/Worker 分离）+ SSE 事件流 + 三层幂等 + lease/sweeper 崩溃恢复 + Sentry/Langfuse/Grafana 可观测 + 故障注入演练 + 数据回流闭环。
>
> 本文档供 coding agent 直接执行。**严格按里程碑顺序推进，每个里程碑末尾有验收门禁（Gate），全部指标通过并记录进 `EXPERIMENTS.md` 后才允许进入下一阶段。**
>
> 部署边界更新（2026-09-07）：常驻运行面已经从 Mac 改为 `home-5090` 的 Docker Compose + RTX 5090；Mac 只作为 SSH/Git 控制面。模型默认改为 Compose 内 Ollama，较大模型可切换 Modal。服务器具体操作以 `docs/REMOTE-OPERATIONS.md` 为准，本文旧的 Mac M0 命令只保留为历史 Gate 证据。

---

## 0. 全局约定（agent 必读）

### 0.1 执行纪律

1. **门禁制**：M0 → M1 → M2 → M3 → M4 顺序执行。每个 Gate 的验收命令必须真实运行，输出结果（或截图说明）写入 `docs/EXPERIMENTS.md`，格式见 §0.4。未过门禁不得写下一阶段代码。
2. **单一变量**：故障注入实验一次只改一个变量；每个实验先写"预测"再执行。
3. **禁止事项**：不引入 Kafka / Celery / K8s / 自部署 Sentry / 自部署 Langfuse v3（全部超出最小范围）；不在 M1 阶段接入任何可观测 SDK（刻意制造"裸奔期"对比）。
4. **提交纪律**：每完成一个里程碑打 git tag（`m1-skeleton` / `m2-observability` / `m3-chaos` / `m4-flywheel`）。
5. 所有服务代码 Python 3.12；类型标注齐全；配置一律走环境变量（pydantic-settings），提供 `.env.example`。

### 0.2 技术栈（与简历对齐，不得替换）

| 层 | 选型 |
|---|---|
| API | FastAPI + Pydantic v2 + uvicorn |
| DB | PostgreSQL 16 + SQLAlchemy 2.0 + Alembic |
| 队列 | Redis 7 + RQ（job id 复用 run_id 做去重） |
| SSE | sse-starlette |
| 模型 | ollama（Compose 内使用 RTX 5090）`qwen3:0.6b`，OpenAI 兼容端点；可切换 Modal |
| 可观测 | OpenTelemetry SDK → `grafana/otel-lgtm` 单容器；Sentry Cloud；Langfuse Cloud |
| 压测 | hey（brew 安装）+ 一个自写并发脚本 |
| 编排 | `home-5090` Docker Compose；api/worker/dispatcher/sweeper 共用同一个 Image，不同启动命令 |

### 0.3 架构总图（心中常驻）

```
浏览器 ──SSE──▶ api ──事务写──▶ PostgreSQL（runs/events/outbox/tool_calls/idempotency/mock业务表）
                │                    ▲            ▲
                └─(只读事件)          │            │
 dispatcher ──扫outbox──▶ Redis(RQ) │            │
 worker ──领活/claim lease──────────┘   chaos-proxy ──▶ 宿主机 ollama:11434
 sweeper ──扫过期lease──▶ 重新入队/判死
 lgtm(Grafana+Loki+Tempo+Prom) ◀──OTLP── 所有服务（M2 起）
```

关键原则：**进程可以死，表不能丢**。业务真相只在 PostgreSQL；Redis 只是传送带；模型只是一个会失败的外部 HTTP 依赖。

### 0.4 EXPERIMENTS.md 记录模板

```markdown
## [M1-G3] SSE 断线补齐验证
- 时间: 2026-xx-xx
- 操作: <执行的命令>
- 预期: <执行前写下的预测>
- 实际: <观察到的输出/数字>
- 结论: PASS / FAIL（FAIL 则附修复记录后重测）
```

---

## M0 · 环境准备（半天）

### 步骤

1. 确认宿主机依赖：
   ```bash
   brew install ollama hey jq
   ollama serve &          # 或 brew services start ollama
   ollama pull qwen3:0.6b
   ```
2. 验证模型端点与 function calling 能力：
   ```bash
   curl -s http://localhost:11434/v1/chat/completions -d '{
     "model": "qwen3:0.6b",
     "messages": [{"role":"user","content":"当前 camp_001 的预算是多少? /no_think"}],
     "tools": [{"type":"function","function":{"name":"get_campaign",
       "description":"查询广告计划详情","parameters":{"type":"object",
       "properties":{"campaign_id":{"type":"string"}},"required":["campaign_id"]}}}]
   }' | jq '.choices[0].message'
   ```
   期望返回 `tool_calls` 字段且函数名/参数正确。
   - ⚠️ 0.6B 工具调用不稳属正常。缓解手段（按序尝试）：`temperature=0`；prompt 内附 `/no_think`；工具数 ≤3；描述精简。若 20 次采样成功率 <70%，**降级预案**：换 `qwen3:1.7b`（显存仍然充裕），并在 EXPERIMENTS.md 记录两个模型的成功率对比——这本身就是一条有价值的实验数据。
3. Docker Desktop 内存上限保持在用户批准的约 7GB（验收按 `docker info` ≥7GiB）；确认容器内可达宿主机：`host.docker.internal`。不得为本实验擅自增配到 10GB；M2 引入 LGTM 后用实测 RSS/OOM 作为继续或停下调优的依据。
4. 注册 Sentry（免费档，建项目拿 DSN）与 Langfuse Cloud（拿 public/secret key），暂存进 `.env`，**M2 之前不接入**。

### Gate M0

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | ollama 端点存活 | `curl localhost:11434/v1/models` 返回 200 |
| G2 | 工具调用成功率 | 同一 prompt 采样 20 次，返回合法 tool_calls 的比例 ≥70%（记录实际数字与所用模型） |
| G3 | Docker 资源 | `docker info` 显示内存 ≥7GiB，且不要求用户增配到 10GB |

---

## M1 · 骨架通车：一条 run 的完整生命周期（1-2 天）

**感受目标**：亲眼看到 created → enqueued → started → tool_call → completed 在浏览器里逐行打出来；kill 掉任何进程，run 不丢。

### 1.1 仓库结构

```
harness-lab/
├── compose.yaml
├── Dockerfile                  # 单 Image
├── .env.example
├── alembic/                    # 迁移脚本
├── app/
│   ├── config.py               # pydantic-settings
│   ├── db.py                   # engine/session
│   ├── models.py               # SQLAlchemy ORM
│   ├── api/main.py             # FastAPI 入口
│   ├── api/routes_runs.py      # POST /runs, GET /runs/{id}, GET /runs/{id}/events (SSE)
│   ├── dispatcher.py           # outbox 扫描循环
│   ├── worker.py               # RQ worker 入口 + agent loop
│   ├── agent/loop.py           # 多轮 tool-calling 循环
│   ├── agent/tools.py          # 3 个 mock 投放工具
│   ├── agent/llm.py            # OpenAI SDK 指向 chaos-proxy/ollama
│   ├── sweeper.py              # lease 过期扫描
│   └── chaos/proxy.py          # M3 用的故障注入代理（先建空壳）
├── web/index.html              # 裸 HTML + EventSource 观察页
├── scripts/                    # 验收/压测/导出脚本
└── docs/EXPERIMENTS.md
```

### 1.2 数据库 Schema（Alembic 首版迁移）

```sql
-- 单一事实源六张表（字段可增不可减，方向对齐 expand-contract）
CREATE TABLE agent_runs (
  id            TEXT PRIMARY KEY,              -- run_xxx (ulid)
  status        TEXT NOT NULL,                 -- queued|running|waiting_approval|completed|failed|cancelled
  input_json    JSONB NOT NULL,
  result_json   JSONB,
  error_code    TEXT,                          -- MODEL_429|MODEL_TIMEOUT|TOOL_ERROR|BAD_OUTPUT|...
  prompt_version TEXT NOT NULL DEFAULT 'v1',   -- 版本快照（最小化：只留一个）
  lease_owner   TEXT,
  lease_expires_at TIMESTAMPTZ,
  attempt       INT NOT NULL DEFAULT 0,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE run_events (
  run_id     TEXT NOT NULL REFERENCES agent_runs(id),
  sequence   INT  NOT NULL,                    -- 每 run 内单调递增，SSE 的 event id
  type       TEXT NOT NULL,                    -- run.created|run.enqueued|run.started|step.model_call|step.tool_call|step.tool_result|run.completed|run.failed|run.heartbeat
  payload    JSONB NOT NULL DEFAULT '{}',      -- public 密级字段 only（数据分类纪律）
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, sequence)
);
CREATE TABLE outbox_jobs (
  id             BIGSERIAL PRIMARY KEY,
  task           TEXT NOT NULL,                -- 'execute_run'
  payload        JSONB NOT NULL,               -- {"run_id": "..."}
  status         TEXT NOT NULL DEFAULT 'pending',  -- pending|dispatched
  attempts       INT NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error     TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_outbox_pending ON outbox_jobs (next_attempt_at) WHERE status = 'pending';
CREATE TABLE tool_calls (
  id               BIGSERIAL PRIMARY KEY,
  run_id           TEXT NOT NULL,
  step             INT NOT NULL,
  tool_name        TEXT NOT NULL,
  args_json        JSONB NOT NULL,
  idempotency_key  TEXT NOT NULL UNIQUE,       -- "{run_id}:{step}:{tool}:{sha256(args)[:12]}"
  status           TEXT NOT NULL,              -- executing|succeeded|failed|unknown
  result_json      JSONB,
  latency_ms       INT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE idempotency_keys (               -- 请求层幂等
  key         TEXT PRIMARY KEY,               -- 客户端 Idempotency-Key header
  run_id      TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE campaigns (                      -- mock 业务表（工具的"世界"）
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  budget      NUMERIC(12,2) NOT NULL,
  spend_today NUMERIC(12,2) NOT NULL,
  status      TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE budget_audit (                   -- 高危操作审计（验证幂等的关键证据表）
  id          BIGSERIAL PRIMARY KEY,
  campaign_id TEXT NOT NULL,
  delta       NUMERIC(12,2) NOT NULL,
  run_id      TEXT NOT NULL,
  tool_call_key TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

种子数据：3 条 campaigns（camp_001/002/003，预算 1000/500/2000）。

### 1.3 API 层

- `POST /runs`：
  1. 读取 `Idempotency-Key` header（可选）：命中 idempotency_keys 直接返回已有 run_id（**请求层幂等**）。
  2. 单事务写三行：`agent_runs(status=queued)` + `run_events(seq=1, run.created)` + `outbox_jobs(pending)`，COMMIT 后返回 `{run_id}`。**事务里绝不碰 Redis。**
  3. 记录 POST 处理耗时（先打日志，M2 换 metrics）。
- `GET /runs/{id}`：状态查询（Polling 兜底）。
- `GET /runs/{id}/events`（SSE）：
  - `id:` 字段 = run_events.sequence；支持 `Last-Event-ID` header，重连时从 `sequence > last_id` 补发（**断线续传**）。
  - 实现：循环查 run_events（500ms 间隔轮询即可，最小实现不上 LISTEN/NOTIFY）+ 每 15s 发 `: heartbeat` 注释行。
  - run 到达终态后推 `run.completed/failed` 然后正常关闭流。
- `web/index.html`：输入框（诊断指令）+ 创建按钮 + 事件流逐行渲染 + 断线自动重连（EventSource 原生行为），显示当前 Last-Event-ID。

### 1.4 Dispatcher（独立进程）

```
循环每 1s:
  BEGIN;
  SELECT * FROM outbox_jobs
   WHERE status='pending' AND next_attempt_at <= now()
   ORDER BY id LIMIT 100
   FOR UPDATE SKIP LOCKED;          -- 多实例安全
  对每条:
    rq.enqueue('execute_run', run_id,
               job_id=f'{run_id}_outbox_{outbox_id}', unique=True)
      # 同一 outbox 行重试会去重；sweeper 的新 outbox 行可产生新投递。
      # run 级任务幂等由数据库 lease_owner + attempt fencing 保证，不能依赖 RQ job_id。
    UPDATE status='dispatched'; INSERT run_events(run.enqueued);
  COMMIT;
失败: attempts+1, next_attempt_at = now() + min(60s, 2^(attempts-1) * 1s), 记 last_error
```

### 1.5 Worker 与 Agent Loop

Worker 领到 job 后：

1. **Claim（先抢 lease 再干活）**：
   ```sql
   UPDATE agent_runs SET status='running', lease_owner=:worker_id,
     lease_expires_at = now() + interval '30 seconds', attempt = attempt + 1
   WHERE id=:run_id AND status IN ('queued','running')
     AND (lease_expires_at IS NULL OR lease_expires_at < now())
   RETURNING id;
   ```
   返回 0 行 = 别人持有 lease，直接放弃（**任务层幂等的第二道**）。
2. 后台线程每 10s 续约 lease（UPDATE lease_expires_at）。
3. Agent loop（上限 6 轮）：
   - 组 messages（system prompt 含工具使用规范 + `/no_think`）→ 调 `app/agent/llm.py`（OpenAI SDK，base_url 指向 `CHAOS_PROXY_URL`，M1 时该 env 直接指 ollama）。
   - 模型返回 tool_calls → 逐个执行；每次模型调用/工具调用各 append 一条 run_events。
   - 模型返回纯文本 → 视为 final answer → `status=completed`，写 result_json。
4. **工具层幂等**：执行副作用工具（adjust_budget）前：
   ```sql
   INSERT INTO tool_calls(..., idempotency_key, status='executing')
   ON CONFLICT (idempotency_key) DO NOTHING RETURNING id;
   ```
   冲突且已 succeeded → 直接返回历史 result_json（**不重复执行**）；冲突且 executing/unknown → 返回"结果未知，挂起"并让 run 进入 failed(TOOL_UNKNOWN)（最小实现；对账回填留作扩展点注释）。
5. 三个工具（直接操作 pg）：
   - `get_campaign(campaign_id)`：读 campaigns（只读，天然幂等）。
   - `get_report(campaign_id)`：返回 mock 消耗数据（只读）。
   - `adjust_budget(campaign_id, delta)`：**高危写**。UPDATE budget + INSERT budget_audit（同事务）。注意：模型只传 delta 意图，上限校验（|delta| ≤ 预算 20%）在工具内硬编码——schema 闸门纪律。
6. 任何异常：写 run_events(run.failed) + error_code 分类 + status=failed。

### 1.6 Sweeper（独立进程）

```
循环每 10s:
  UPDATE agent_runs SET status='queued', lease_owner=NULL
  WHERE status='running' AND lease_expires_at < now() - interval '5 seconds'
  RETURNING id;
  对每条: INSERT outbox_jobs(execute_run) -- 走正门重新入队, attempt 保留
         INSERT run_events(run.requeued_by_sweeper)
  若 attempt >= 3: 改判 status='failed', error_code='MAX_RETRY'
```

### 1.7 Compose（M1 版）

```yaml
services:
  postgres:
    image: postgres:16
    environment: { POSTGRES_PASSWORD: harness, POSTGRES_DB: harness }
    ports: ["5432:5432"]
    volumes: [./data/postgres:/var/lib/postgresql/data]
    healthcheck: { test: ["CMD-SHELL","pg_isready -U postgres"], interval: 3s, retries: 10 }
  redis:
    image: redis:7
    ports: ["6379:6379"]
    volumes: [./data/redis:/data]
  api:
    build: .
    command: uvicorn app.api.main:app --host 0.0.0.0 --port 8000
    ports: ["8000:8000"]
    env_file: .env
    depends_on: { postgres: { condition: service_healthy }, redis: { condition: service_started } }
  dispatcher:
    build: .
    command: python -m app.dispatcher
    env_file: .env
    depends_on: { postgres: { condition: service_healthy }, redis: { condition: service_started } }
  worker:
    build: .
    command: python -m app.worker
    env_file: .env
    deploy: { replicas: 1 }
    depends_on: { postgres: { condition: service_healthy }, redis: { condition: service_started } }
  sweeper:
    build: .
    command: python -m app.sweeper
    env_file: .env
    depends_on: { postgres: { condition: service_healthy } }
```

`.env` 关键项：`DATABASE_URL`、`REDIS_URL`、`LLM_BASE_URL=http://host.docker.internal:11434/v1`、`LLM_MODEL=qwen3:0.6b`、`LEASE_SECONDS=30`、`MAX_ATTEMPTS=3`。

### 1.8 验收脚本

`scripts/verify_m1.sh`：
1. 循环创建 20 个 run（prompt："诊断 camp_001 今日消耗并汇报预算使用率"），轮询至终态，统计成功率与耗时分布。
2. 用 curl 手工测 SSE：`curl -N -H "Last-Event-ID: 2" localhost:8000/runs/<id>/events`，校验从 seq=3 开始补发。
3. 幂等冒烟：同一 `Idempotency-Key` 连发 3 次 POST → 只产生 1 个 run（`SELECT count(*)`验证）。

### Gate M1（全部 PASS 才进 M2）

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 端到端成功率 | 20 个连续 run，completed 比例 ≥ 90%（0.6B 智力导致的 BAD_OUTPUT 允许 ≤2 个，但不允许任何系统性错误） |
| G2 | 创建延迟 | POST /runs 事务提交并返回 run_id 耗时 p95 < 100ms（脚本统计 20 次） |
| G3 | SSE 断线补齐 | 断开后带 Last-Event-ID 重连，事件序列无缺号无重复（脚本比对 sequence 连续性） |
| G4 | 请求层幂等 | 同 Idempotency-Key 3 连发 → agent_runs 仅 1 行 |
| G5 | 崩溃不丢 run | 手动 `docker kill harness-lab-worker-1`（run 进行中）→ ≤60s 内 sweeper 重新入队 → run 最终 completed；budget_audit 中该 run 的同一 tool_call_key **恰好 1 行** |
| G6 | Redis 宕机降级 | `docker stop redis` 期间 POST /runs 仍返回 200（outbox 兜底）；`docker start redis` 后 ≤30s 积压 run 全部被 dispatch |

---

## M2 · 可观测接入：给系统装仪表盘（1-2 天）

**感受目标**：M1 里你靠 `docker logs` 和 psql 肉眼查状态；M2 结束后同样的问题全部一眼看面板。请刻意体会这个对比，写一段 ≤5 行的主观感受进 EXPERIMENTS.md（这是本项目唯一的主观验收项）。

### 步骤

1. compose 增加：
   ```yaml
   lgtm:
     image: grafana/otel-lgtm
     ports: ["3000:3000", "4317:4317", "4318:4318"]
   ```
2. 全服务接 OpenTelemetry：
   - `opentelemetry-instrument` 自动埋 FastAPI / SQLAlchemy / requests / redis；OTLP endpoint 指向 `http://lgtm:4317`。
   - trace 贯通的关键：dispatcher 入队时把 traceparent 塞进 RQ job meta，worker 取出接续 —— 一条 run 的 API→dispatch→worker→tool 全链在 Tempo 里是**一棵树**。
3. 自定义 metrics（OTel Meter，命名与简历对齐）：
   - `agent_queue_lag_seconds`（histogram：worker claim 时 now − run.created_at）
   - `agent_run_duration_seconds`（histogram，按 status 标签）
   - `agent_run_total` / `agent_run_failed_total`（counter，带 error_code 标签）
   - `agent_tool_latency_ms`（histogram，按 tool_name）
   - `agent_model_call_total`（counter，按 http_status：200/429/timeout）
   - `outbox_pending_jobs` / `stuck_runs_swept_total`（dispatcher/sweeper 上报）
4. Grafana 建一个 dashboard「Harness SLO」四块面板，阈值线直接画上：
   - Run P95 duration（SLO 线 60s）
   - run_failed_rate（SLO 线 1%）
   - queue_lag_seconds P95（SLO 线 10s）
   - model 429/timeout rate + tool latency by name
   - 两条告警规则：queue_lag_p95 > 10s 持续 2min；failed_rate > 5% 持续 2min（通知渠道用 Grafana 内置即可）。
5. Langfuse SDK 包住 `app/agent/llm.py` 的每次模型调用（trace_id 用 run_id），看每轮 prompt/completion/latency。
6. Sentry SDK 接入 api + worker（两行初始化），故意抛一个测试异常验证上报。
7. `GET /metrics/debug` 内部端点：返回 pending outbox 数、running 数、各状态计数（runbook 定位用）。

### Gate M2

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | trace 完整性 | Tempo 中任选一个 run 的 trace，span 覆盖 api→dispatcher→worker→每次 model/tool 调用，无断链 |
| G2 | 指标可读 | 跑 30 个 run 后，dashboard 四块面板全部有数据，能读出 queue_lag P95 / run P95 / failed_rate 三个具体数字（记入 EXPERIMENTS.md 作为**基线**） |
| G3 | Langfuse | 任选一个 run，能看到全部模型轮次的 prompt 与 token 统计 |
| G4 | Sentry | 测试异常出现在 Sentry issue 列表，含 run_id 上下文 |
| G5 | 告警通路 | 手动停掉全部 worker 触发 queue_lag 告警 → Grafana alert 进入 Firing 状态 |

---

## M3 · 故障注入演练：把每条防护打一遍（2-3 天，核心阶段）

**感受目标**：简历上每一条防护 ↔ 一次亲眼所见的故障现场。每个实验按 §0.4 模板记录，**先写预测再动手**。

### 3.1 Chaos Proxy（先建）

`app/chaos/proxy.py`：一个 80 行左右的 FastAPI 反向代理，转发到 ollama，按 env 注入故障：

```
CHAOS_429_RATE=0.0        # 概率返回 429
CHAOS_TIMEOUT_RATE=0.0    # 概率挂起 60s（触发客户端超时）
CHAOS_LATENCY_MS=0        # 每次附加延迟
CHAOS_5XX_RATE=0.0
```

compose 增加 chaos-proxy 服务；`.env` 里 `LLM_BASE_URL` 改指 `http://chaos-proxy:9000/v1`。llm.py 客户端配置：timeout 30s、429/5xx 指数退避重试 ≤3 次（retry 事件写 run_events）。

### 3.2 实验清单（每个都是"预测→注入→看面板→对照防护→记录"）

**EXP-1 · worker 猝死（防护：lease + sweeper + 工具幂等）**
- 注入：创建一个必然调用 adjust_budget 的 run；在 worker 日志出现 `tool executing` 的瞬间 `docker kill` worker 容器；重复整个流程 **10 次**。
- 观察：sweeper 日志、run_events 里的 requeued 事件、Grafana stuck_runs_swept_total。
- 通过标准：10 次中 run 最终 completed ≥ 9 次；**budget_audit 中每个 tool_call_key 严格 = 1 行（0 次双扣）**；恢复时间（kill → 重新 running）≤ 45s（lease 30s + sweeper 周期）。

**EXP-2 · dispatcher 在"投递后、标记前"死亡（防护：at-least-once + 任务层幂等）**
- 注入：在 dispatcher 代码 publish 与 UPDATE 之间加一个 `CHAOS_DISPATCHER_CRASH=1` 时 `os._exit(1)` 的开关；触发一次后重启 dispatcher。
- 通过标准：outbox 该行仍 pending → 被重投；同一 Outbox delivery 由当前 `{run_id}_outbox_{outbox_id}` job ID 去重，run 级所有权由 PostgreSQL lease/fencing 拒绝重复 claim，**run 只执行一份**（run_events 无重复 started 序列）。

**EXP-3 · Redis 全宕 60s（防护：Outbox 缓冲）**
- 注入：持续以 1 run/s 创建（脚本），期间 `docker stop redis` 60 秒后恢复。
- 通过标准：宕机期间 POST 成功率 100%；恢复后所有积压 run 在 ≤ 2 分钟内消化完（看 outbox_pending_jobs 曲线归零）；全程 0 个 run 丢失（创建数 = 终态数）。

**EXP-4 · 模型 provider 退化（防护：重试退避 + 错误分类 + 告警）**
- 注入：`CHAOS_429_RATE=0.5` 跑 30 个 run；再 `CHAOS_TIMEOUT_RATE=0.3` 跑 30 个。
- 通过标准：面板 model_call 429 曲线清晰可见；重试后最终成功率 ≥ 70%；失败 run 的 error_code 正确分类为 MODEL_429/MODEL_TIMEOUT（`SELECT error_code, count(*)`）；queue_lag 告警在退化期间 Firing、恢复后 Resolved。**记录：429 注入前后 run P95 的对比数字。**

**EXP-5 · 压测找拐点（防护：API/Worker 分离 + 独立扩缩容）**
- 注入：`hey -n 500 -c 50 -m POST ...` 打 /runs（配 CHAOS_LATENCY_MS=300 模拟真实模型速度）。
- 观察：API P95（应保持 <100ms，**创建与执行解耦的直接证据**）；queue_lag 爬升曲线与拐点并发数。
- 然后 `docker compose up -d --scale worker=4`，同压力重打。
- 通过标准：API P95 在两轮中均 <150ms；worker 1→4 后 queue_lag P95 下降 ≥ 50%（记录两组数字）；给出"单 worker 饱和吞吐 ≈ X run/min"的实测结论。

**EXP-6 · SSE 断线风暴（防护：Last-Event-ID 补齐）**
- 注入：脚本开 20 个 SSE 连接，随机断开重连（带 Last-Event-ID）各 5 次。
- 通过标准：所有客户端最终收到的 sequence 集合无缺号、无重复（脚本断言）。

### Gate M3

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 六个实验全部 PASS | EXPERIMENTS.md 有 6 条完整记录（预测/实际/数字） |
| G2 | 零双重副作用 | 全部实验累计：budget_audit 无任何重复 tool_call_key（一条 SQL 全局验证） |
| G3 | 基线对比表 | 产出一张「正常 / 429退化 / 压测 / 扩容后」四列 × 「run P95 / queue_lag P95 / failed_rate」三行的实测数字表 |

---

## M4 · 数据回流闭环：从轨迹到评测集（1 天）

**感受目标**：跑通飞轮最小一圈——线上轨迹 → 归因 → 清洗 → 评测集，亲手体会"环境错误不进训练负样本"这条纪律的执行成本。

### 步骤

1. 批量生成素材：50 个 run，构成刻意混合——30 个正常指令、10 个开着 `CHAOS_429_RATE=0.3`（制造环境错误）、10 个诱导性差指令（如"把 camp_001 预算清零"——应被工具上限校验拒绝，制造 policy 层拒绝样本）。
2. `scripts/export_traces.py`：从 run_events + tool_calls 重建每个 run 的完整轨迹 JSON（messages 格式 + 工具调用序列 + 终态）。
3. 归因分类（写进每条轨迹的 `label` 字段）：
   - `env_error`：error_code ∈ {MODEL_429, MODEL_TIMEOUT, TOOL_5XX} → **排除，不进任何训练负样本**（分类正确性是本阶段的灵魂）
   - `good`：completed 且 budget_audit 校验合法 → 候选正样本
   - `bad_behavior`：模型行为问题（越权尝试、参数错误、BAD_OUTPUT）→ 候选负样本/评测题
4. 脱敏过程走一遍形式：campaign 名替换占位符（本项目无真 PII，但管线要有这一步）。
5. 产出 `eval/dataset_v1.jsonl`：每条含 input / expected_behavior（人工给 10 条 bad case 写期望）/ source_run_id / label。
6. `scripts/replay_eval.py`：将 eval 集重新喂给当前系统跑一遍，输出 pass/fail 报告——**回归测试雏形**。

### Gate M4（同时是全项目验收）

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 归因准确 | 抽查 15 条轨迹人工核对 label，分类准确率 ≥ 90%，env_error 零漏判进负样本 |
| G2 | 评测集成型 | dataset_v1.jsonl ≥ 20 条，含 ≥5 条带 expected_behavior 的 bad case，全部可追溯 source_run_id |
| G3 | 回归可跑 | replay_eval.py 输出报告，通过率有具体数字 |
| G4 | 全项目复盘 | EXPERIMENTS.md 末尾补一节「与简历逐条对照」：简历 Project 2 每一条 bullet ↔ 本项目哪个实验/哪个数字提供支撑，逐条打勾 |

---

## 附录 A · 常用运维命令速查

```bash
docker compose up -d --build          # 全量启动
docker compose down -v                # 含数据卷的彻底重置（每个大实验前的干净起点）
docker compose logs -f worker         # 追日志
docker compose up -d --scale worker=4 # 扩容
psql postgresql://postgres:harness@localhost:5432/harness   # 直连查表
# 三条最常用的排查 SQL:
# SELECT status, count(*) FROM agent_runs GROUP BY 1;
# SELECT * FROM outbox_jobs WHERE status='pending' ORDER BY id DESC LIMIT 10;
# SELECT tool_call_key, count(*) FROM budget_audit GROUP BY 1 HAVING count(*) > 1;  -- 永远应为空
```

## 附录 B · 预估工时与风险

| 阶段 | 预估 | 主要风险与预案 |
|---|---|---|
| M0 | 0.5 天 | 0.6B 工具调用不稳 → 降级 1.7B（已内置预案） |
| M1 | 1-2 天 | SSE 轮询实现的边界条件（终态后关流）→ 验收脚本已覆盖 |
| M2 | 1-2 天 | RQ 跨进程 trace 接续 → 用 job meta 传 traceparent（已写明） |
| M3 | 2-3 天 | 实验记录偷懒 → 门禁强制 6 条完整记录 |
| M4 | 1 天 | 归因分类含糊 → G1 抽查 15 条硬标准 |

总计：认真做约 6-8 天；配合 coding agent 可压缩至 3-4 天。
