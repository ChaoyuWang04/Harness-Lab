# Harness Learn · 灾难观测与诊断学习平台施工手册

> 目标：在已通过 M3 的 Harness Lab 上，补齐软件层可控故障、全链可观测和恢复手册，最后建设“渐进课程 + 完全盲测”双模块学习入口。学习者不只看到服务报错，而是能沿着统一时间轴回答：**谁受影响、故障在哪一层、保护机制做了什么、何时恢复、数据是否仍然正确。**
>
> 本文是 Learn Platform 的唯一阶段计划，阶段编号使用 **L0 → L8**，不占用原 `docs/harness-lab-plan.md` 中的数据回流 M4。优先级更新（2026-09-08）：先完成原计划 M4 的轨迹到评测闭环，再开始 L0；两条线不并行施工。
>
> 本文供 coding agent 施工。每阶段必须先注册场景和判据，再按测试驱动实现；Gate 的机器证据写入 `docs/EXPERIMENTS.md`，原始脱敏证据写入 `artifacts/learn/<drill_id>/`。前一 Gate 未通过，不进入下一阶段。

---

## 0. 已批准的范围与完成定义

### 0.1 平台边界

平台只真实注入**软件可以隔离、限时并自动回滚**的故障：

1. **安全自动级**：Worker、Dispatcher、独立 Redis、provider 429、Timeout、5xx、坏模型输出、负载、SSE。
2. **隔离实验级**：PostgreSQL 断连/延迟、API replica 崩溃、Sweeper 停止、CPU/内存/I/O 压力、网络丢包。
3. **人工监督级**：宿主机重启、真实磁盘耗尽、Docker daemon 崩溃、GPU/驱动重置、整机断网或断电等只写识别与处置说明，标记 `simulation_only=true`，**不由平台执行，也不纳入学习考试题库**。

所有代码、配置、模型引用、文档、日志、数据和中间产物继续限制在独立 `harness-lab/` 仓库；常驻和实验运行面在 `home-5090`，Mac 只作 Git/SSH/浏览器控制面。

### 0.2 “全部完成”的定义

只有同时满足以下条件，Learn Platform 才能宣告完成：

- 17 个软件故障场景逐项通过确定性注入、保护、恢复与数据一致性 Gate；
- Grafana、Prometheus、Loki、Tempo、Langfuse、Sentry 和 PostgreSQL 事件可用同一 `drill_id` 或同一时间窗互相定位；
- 每个场景都有“灾难 → 现象 → 鉴别 → 自动保护 → 手动恢复 → 一致性确认”Runbook；
- 唯一前端调试页同时提供普通 run、系统拓扑、统一时间轴、课程和盲测，不新增第二入口；
- 渐进课程和完全盲测均通过端到端验收；
- 任一注入都不能逃逸到普通 `harness` 数据库、普通 Redis、宿主机网络或其他项目容器；
- 所有实际注入均有 TTL、单场景锁、自动回滚和失败兜底；回滚成功率必须为 100%。

### 0.3 不做什么

- 不把公开 API 变成任意 shell/Docker 控制台；不接受用户传入容器名、命令、概率或持续时间。
- 不把 Docker socket 挂给公开 API；若控制器需要容器能力，只允许白名单场景和精确目标。
- 不在 Prometheus 指标标签中写高基数 `drill_id`，也不写会给盲测泄题的 `scenario_id`；指标通过有界 `service`/`target_role`/`exercise_mode` 标签、exemplar 和时间窗关联。
- 不把计划内故障默认制造成 Sentry issue。Sentry 用于捕获**未预期异常、保护机制失败和恢复失败**。
- 不以 LLM 主观判断作为唯一考试评分器；核心分数来自结构化答案和确定性证据。
- 不把按钮演示当作 Gate 证据，也不改写已经通过的 M3 结果。

---

## 1. 总体架构

### 1.1 单前端调试台与隔离后端

```text
浏览器：Harness Debug Console（唯一页面、唯一 URL）
  └── 页面内模式：Run / 观察课 / 引导练习 / 独立练习 / 盲测
      共用组件：系统拓扑 / 统一时间轴 / 黄金信号 / 证据抽屉
                         │
                         ▼
                  Mode/API Router（无 Docker socket）
       ┌─────────────────┴─────────────────┐
       ▼                                   ▼
Operator/课程：白名单 scenario_id      盲测：只提交 start exam
                                           │ 场景/seed 仅服务端选择
       └─────────────────┬─────────────────┘
                         ▼
     Drill Controller + 独立 Reconciler（白名单、单锁、TTL、回滚）
                         │
       ┌─────────────────┴──────────────────┐
       ▼                                    ▼
隔离学习运行面                         证据与可观测平面
learn DB/schema、learn Redis、          PostgreSQL drill_events
learn API/worker/dispatcher/sweeper、   Prometheus/Grafana、Loki、Tempo、
provider/toxiproxy、受限压力进程         Langfuse、Sentry
```

- **前端边界**：只保留现有 `web/index.html` 和现有 API/UI 地址，不新建 Learn 首页、第二个 SPA、第二个前端端口或独立考试 URL。普通 run 与教学功能都是同一个调试台里的页面状态；切换模式不触发整页导航。
- **控制平面**只接收版本化场景 ID；实际命令、精确容器、持续时间、停止线和恢复动作都来自仓库内受审查的 manifest。Controller 负责启动，独立 Reconciler 每 2 秒检查 PostgreSQL 中的 active drill lease；Controller 被 `SIGKILL`、OOM 或重启后，Reconciler 仍会在 TTL 到期时回滚。Controller/Reconciler 启动时都先收敛遗留 active drill。
- **实验平面**使用 `harness_learn` 数据库、独立 Redis 和带 `com.docker.compose.project=harness-learn` 身份的服务。PostgreSQL 断连/延迟通过 learn 应用与数据库之间的代理注入，不停止共享 PostgreSQL 服务。
- **观测平面**可以复用 Lab 的 LGTM、Sentry Cloud 和 Langfuse Cloud。workload telemetry 带 `environment=learn` 和不透明 `drill_id`；语义化 `scenario_id` 只留在 Operator Evidence Vault，防止盲测泄题。
- 学习服务按场景按需启动，平台总 RSS 继续遵守现有 `<4 GiB` 停止线；达到停止线立即中止注入并回滚，不以扩容规避 Gate。

所有注入原语还必须有第二层自失效能力：压力进程和网络 gateway 由自身 `timeout` 退出；容器崩溃只控制带 restart policy 的 learn 副本；Toxiproxy toxic 同时登记绝对过期时间供 Reconciler 清理。Controller 自己的 `finally` 只是第一层，不是唯一恢复机制。

### 1.1.1 首版组件冻结

| 目的 | 首版选型 | 边界 |
|---|---|---|
| Docker 日志采集 | Grafana Alloy 1.19 | 只读 Docker 日志/metadata，写入现有 Loki |
| 容器资源指标 | cAdvisor 0.60.5 | 只读 cgroup/Docker 状态，写入现有 Prometheus |
| API 双副本入口 | HAProxy 3.4.4 LTS | 固定 `learn-api-a/b`，1 秒健康检查，`fall 2` / `rise 2` |
| DB 断连/延迟 | Toxiproxy 2.12.0 | 只代理 learn 数据库连接，不代理普通 DB |
| 网络丢包 | 独立 netem gateway | `NET_ADMIN` 只授予临时 gateway 容器，不授予 API/controller |
| CPU/内存/I/O | learn-only worker image 内的 `stress-ng` / `fio` | Controller 只可在精确 learn worker cgroup 执行固定命令模板，自带 timeout |

具体 image 必须在 L0 以不可变 digest 锁定并写入 `config/learn/components.lock.yaml`；项目自有 injector image 同时锁定 `stress-ng`、`fio` 和 `iproute2` 包版本。若目标机兼容性实测失败，先记录证据并修订本文选型，不在执行命令里静默替换。

### 1.2 Drill 生命周期

```text
CREATED → BASELINING → ARMED → INJECTING → DEGRADED
                                      │
                                      ▼
ABORTED ← VERIFYING ← RECOVERING ← PROTECTED
                    │
                    └──────────────→ PASSED / FAILED
```

每次状态变化都追加一条不可覆盖的 `drill_events`，至少包含：

- `drill_id`、`scenario_id`、`sequence`、`phase`、UTC 时间；
- 精确目标身份和场景版本；
- 注入见证、首次用户症状、首次根因信号、保护动作、恢复动作；
- 一致性查询名称、摘要结果和证据文件路径；
- 失败原因、自动回滚结果和人工接管标记。

一条完整时间轴固定为：

```text
故障前基线 → 注入 → 检测 → 防护动作 → 恢复 → 数据一致性确认
```

### 1.3 统一关联规则与盲测证据分层

| 系统 | 关联方式 | 验收方式 |
|---|---|---|
| PostgreSQL | workload 保存 `drill_id`；受限 control schema 保存 `drill_id → scenario_id` | 按 ID 重建有序时间轴 |
| Trace/Tempo | workload span 只带不透明 `harness.drill.id`；controller span 属于 operator-only | 从 drill 页面打开对应 trace |
| Loki | workload JSON 含 `drill_id`、`trace_id`、service；injector/controller log 属于 operator-only | 10 秒内可检索哨兵日志 |
| Langfuse | trace/session metadata 保存不透明 `drill_id`；场景映射留在 control schema | 从 drill 页面打开模型轮次 |
| Sentry | 仅异常事件 tag `drill_id`、`run_id`；场景映射由 operator 侧解析 | 未预期异常能反向回到 drill |
| Prometheus/Grafana | 只用 `service`/`target_role`/`exercise_mode` 等有界标签 + 时间窗/exemplar | drill 页面带起止时间跳转 |

所有来源统一使用 UTC；同一状态在 `drill_events` 中只写一次且 sequence 单调。跨来源时间点允许最多 ±5 秒偏差，不能依靠前端猜测顺序。

证据分为两面：

- **Operator Evidence Vault**：保存场景映射、injector/controller 日志、原始 datasource 链接、完整 artifact 和 Runbook；用于 Gate 与提交后讲解。
- **Student Evidence API**：提交前只执行服务端白名单 query，返回 workload 指标、业务日志、trace span、模型轮次和只读 DB 摘要；移除 `scenario_id`、seed、injector、control-plane span/log、Runbook hash 和答案字段。不得向浏览器返回 Grafana datasource credentials 或原始 artifact URL。

盲测的威胁模型是“考生只使用调试台的盲测模式和学生凭据”；宿主机 SSH、Operator Grafana 账号和服务器文件系统天然属于监考员权限，不能阻止拥有者主动作弊，但同一前端切入盲测模式后不得把这些权限或链接暴露给考生。

身份和路由从 L0 起硬分离：

- `/api/operator/**` 的长期 Operator credential 只存在 `secrets/.env`，仅供服务器内部和 SSH 控制脚本使用，永不发送到浏览器。SSH 控制脚本可签发一次性、单次兑换、60 秒过期的 bootstrap code；同一调试页兑换后只得到短期、可撤销、HttpOnly + SameSite=Strict 的 operator session；
- `/api/student/**` 使用服务端签发、绑定单个 exam/drill 的短期 session token，只能读取 Student Evidence API 和提交答案；
- 进入盲测时，服务端在同一事务中撤销当前 operator session、创建 exam、服务端选择 scenario/seed，再签发 exam-bound student session；前端先终止全部 operator fetch/SSE，并用 session epoch 丢弃切换前迟到的响应。盲测 start 请求和响应都不包含 scenario_id 或 seed；
- 两类 session 不共用 cookie 名、localStorage key 或响应 schema。学生提交后，服务端签发只读、仅绑定该 drill、30 分钟过期的 reveal token；它不能启动/停止场景，也不能读取其他 drill；返回 Operator 模式必须重新用一次性 bootstrap code 建立新 session；
- `artifacts/` 不由静态 Web server 暴露；Grafana datasource proxy、Tempo/Loki 原始查询和 Operator Vault 只在 operator 路由后。

这些是同一个前端根据当前模式调用的后端权限面，不对应不同网页。进入盲测时，前端还必须清除内存、DOM 和浏览器缓存中的 Operator 数据；授权安全以服务端 session 撤销为准，不能只依赖前端清理。

### 1.4 预定目录

```text
harness-lab/
├── app/drills/                 # catalog、controller、lifecycle、evidence、grading
├── config/learn/
│   ├── scenarios.yaml          # 17 个可执行场景的唯一参数源
│   ├── curriculum.yaml         # 课程顺序、提示和先修关系
│   ├── rubrics.yaml            # 结构化评分规则
│   └── components.lock.yaml    # 组件版本、image digest 与来源
├── config/grafana/             # 扩展 dashboard、alert、datasource 配置
├── docs/learn/
│   ├── README.md               # Runbook 导航与统一术语
│   ├── runbooks/               # 17 个软件故障 Runbook
│   └── manual-only/            # 机器级事故说明，不可执行
├── scripts/
│   ├── verify_learn_*.py       # 各 Gate 固定验收入口
│   └── run_verify_learn_home5090.sh
├── tests/                      # 单元、结构、集成和 UI 测试
└── artifacts/learn/<drill_id>/ # 不提交；每次演练的原始证据
```

`artifacts/learn/<drill_id>/` 至少含 `manifest.json`、`timeline.jsonl`、`evidence.json`、`recovery.json`；考试再增加 `submission.json` 和 `grade.json`。manifest 固定保存 catalog SHA、代码提交 SHA、dashboard UID/version、query-set version、Runbook hash、组件 lock SHA 和脱敏策略版本。文件不得包含密钥、完整模型 prompt 或未经裁剪的用户文本。

---

## 2. 故障目录与覆盖矩阵

### 2.1 安全自动级：9 个场景

| ID | 故障 | 隔离目标 | 主要保护 | 必须看见的关键证据 |
|---|---|---|---|---|
| SA-01 | Worker 在工具提交后崩溃 | learn worker | lease + sweeper + 工具幂等 | requeue、恢复耗时、audit 恰好一次 |
| SA-02 | Dispatcher 投递后提交前崩溃 | learn dispatcher | Outbox + delivery/run fencing | pending 重投、started/terminal 各一次 |
| SA-03 | Redis 不可用 | 独立 learn Redis | PostgreSQL Outbox | POST 仍成功、pending 上升、恢复后归零 |
| SA-04 | provider 429 | provider proxy | 有界重试/退避 | 429、retry、终态分类、告警恢复 |
| SA-05 | provider Timeout | provider proxy | timeout + 有界重试 | 超时 span、retry、MODEL_TIMEOUT |
| SA-06 | provider 5xx | provider proxy | 有界重试/熔断边界 | 5xx 比率、重试、MODEL_5XX |
| SA-07 | 坏模型输出 | provider proxy | schema 校验 + BAD_OUTPUT | 原始内容哈希、校验失败、无工具副作用 |
| SA-08 | 负载突增 | learn API/queue | API/Worker 解耦 + 扩缩容 | RPS、P95、队列拐点、worker 占用 |
| SA-09 | SSE 断线/重连风暴 | learn SSE client | Last-Event-ID 补齐 | 重连次数、无缺号、无重复 |

SA-01 至 SA-05、SA-08、SA-09 复用 M3 已证明机制，但必须接入新 `drill_id`、统一时间轴和新证据契约后重新通过 Learn Gate；不得复制旧实验结果冒充新 Gate。

### 2.2 隔离实验级：8 个场景

| ID | 故障 | 注入边界 | 主要保护/学习目标 | 禁止事项 |
|---|---|---|---|---|
| IX-01 | PostgreSQL 断连 | learn DB proxy | 快速失败、连接恢复、无半事务 | 不停止普通 PostgreSQL |
| IX-02 | PostgreSQL 延迟 | learn DB proxy | DB latency 定位、超时预算 | 不修改宿主网络 |
| IX-03 | API replica 崩溃 | 两个 learn API replica 之一 | 副本接管、客户端重试边界 | 不杀普通 API |
| IX-04 | Sweeper 停止 | learn sweeper | lease 积压可见、恢复后重扫 | 不隐藏“暂未恢复”状态 |
| IX-05 | CPU 压力 | learn worker 单核 cgroup | saturation 与执行延迟关联 | 不运行宿主级 stress |
| IX-06 | 内存压力 | 512MiB 限额的 learn worker | working set/limit/降级 | 不触发目标或非目标 OOM |
| IX-07 | I/O 压力 | learn worker 专用 `/learn-stress` 卷 | I/O wait 与执行延迟鉴别 | 不触碰 DB 卷、不写满真实磁盘 |
| IX-08 | 网络丢包 | learn worker → provider 的 netem gateway | loss/retry/timeout 鉴别 | 不修改宿主默认路由 |

### 2.3 预注册量化矩阵

以下数字是首版固定 Gate，不得等运行后再选择。`检测` 指根因信号首次进入证据查询，`保护` 指预期防护动作出现，`恢复` 从停止注入算到健康和一致性确认；除表内另注外，telemetry 查询可见时限均为 15 秒。

| ID | 样本与故障强度 | 持续 | 检测 / 保护 / 恢复 SLO | 核心数据断言 |
|---|---|---:|---|---|
| SA-01 | 10 个必调写工具的 run；每次在 tool commit 后杀 worker | 瞬时 ×10 | 15s / requeue≤45s / terminal≤90s | 10/10 terminal；每个 tool key audit=1 |
| SA-02 | 10 个 run；每次 enqueue 后、DB commit 前杀 dispatcher | 瞬时 ×10 | 10s / redispatch≤30s / terminal≤60s | 每 run started=1、terminal=1、outbox dispatched |
| SA-03 | Redis down 时 1 POST/s，共 60 个 | 60s | 15s / POST 60/60 成功 / pending=0≤120s | 创建数=终态数=60，丢失=0 |
| SA-04 | 30 个 run，确定性 50% HTTP 429 | 整个批次 | 15s / retry event≤5s / alert Resolved≤180s | 成功率≥70%；耗尽错误 100%=`MODEL_429` |
| SA-05 | 30 个 run，确定性 30% 请求挂到 client timeout+1s | 整个批次 | client timeout+5s / retry≤5s / Resolved≤180s | 成功率≥70%；耗尽错误 100%=`MODEL_TIMEOUT` |
| SA-06 | 30 个 run，确定性 30% HTTP 503 | 整个批次 | 15s / retry≤5s / clean run 10/10≤60s | 成功率≥70%；耗尽错误 100%=`MODEL_5XX` |
| SA-07 | 缺字段/错类型/非法 tool args/截断 JSON 各 10 次 | 每次 1 响应 | 15s / schema reject≤5s / clean run 10/10≤60s | 40/40=`BAD_OUTPUT`；tool/audit 副作用=0 |
| SA-08 | 1 worker 与 4 worker 各 500 POST，c=50，provider +300ms | 每臂至终态 | API 可见≤10s / 扩容≤30s / pending=0≤30min | POST P95<150ms；4-worker queue P95 降≥50%；两臂 500/500 terminal |
| SA-09 | 20 clients × 每个 5 次断连重连 | 至 run 终态 | reconnect≤5s / resume≤5s / drain≤120s | 20/20 与 DB sequence 相同；缺号=0、重复=0 |
| IX-01 | DB proxy disable；10 个带 Idempotency-Key 的 POST 在故障中失败，恢复后各重试 | 30s | 10s / HTTP 503≤5s / DB ready≤60s | 半事务=0；重试后恰好 10 runs/10 outbox |
| IX-02 | DB 往返附加延迟≥400ms；50 POST，c=5；DB connect/statement timeout 固定 2s | 60s | 15s / `db_dependency_slow`≤15s / latency回基线±20%≤60s | 无半事务/重复；请求总数守恒；P95 比基线上升≥350ms |
| IX-03 | HAProxy 后 2 replica；600 请求/60s，其中 300 个幂等 POST，t=10s 杀 1 个 | 60s | health down≤3s / 摘除≤3s / 2/2 healthy≤30s | 总成功率≥99%；300 keys 恰好 300 runs；重复=0 |
| IX-04 | 停 sweeper；10 个 run 在 worker crash 后留下过期 lease | 90s | heartbeat missing≤15s / 明确不假恢复≥45s / 重启后 terminal≤60s | 停止期 requeue=0；恢复后 10/10 terminal；副作用不重复 |
| IX-05 | 目标容器 CPU≥90% 单核配额 | 60s | 15s / saturation alert≤30s / CPU回基线+10pp≤30s | 非目标 restart/OOM=0；实验请求终态守恒 |
| IX-06 | 512MiB hard limit 内 working set 维持 80%–90% | 60s | 15s / memory alert≤30s / working set回基线+10%≤60s | 目标及非目标 OOM=0；临时分配完全释放 |
| IX-07 | learn worker 的 `/learn-stress` 固定 256MiB 文件，4KiB randwrite，限 1000 IOPS | 60s | 15s / I/O alert≤30s / IOPS回基线+20%≤30s | 实测≥800 IOPS；目录总量<300MiB；非目标 I/O P95 增幅<20% |
| IX-08 | gateway 对 workload 路径注入 30% packet loss；200 请求，c=10 | 60s | 15s / retry/timeout≤15s / loss<1%≤30s | 请求终态守恒；无重复 run/副作用；宿主规则指纹不变 |

全局停止线：Lab 总 RSS `<4 GiB`、非注入 OOM=0、宿主剩余内存 `>16 GiB`、普通运行面 learn 行数=0。任一停止线触发即中止本场景并执行恢复，结果记 FAIL。

IX-03 的唯一流量入口固定为 HAProxy。健康检查每 1 秒一次，连续 2 次失败摘除，连续 2 次成功恢复；最多重试 1 次，且只对连接建立失败或返回前空响应重试。所有 POST 必须带唯一 `Idempotency-Key`，连接复用开启；同一 key 无论 ingress 重试多少次只能产生一个 run。

### 2.4 每场景通用验收契约

每个场景必须同时通过以下六类断言：

1. **注入命中**：正对照证明预定目标和故障强度真实发生；只改配置不算命中。
2. **范围隔离**：普通 `harness` 数据库无 learn run；普通 Redis/容器与其他项目未被操作；目标身份逐项匹配。
3. **症状可见**：至少一个黑盒用户症状、一个白盒根因信号、一个保护动作信号。
4. **恢复有界**：达到场景 manifest 的 `recovery_slo_seconds`，且 TTL 到期或执行异常时都自动回滚。
5. **数据正确**：run 数量守恒、事件 sequence 连续、无重复副作用、无半提交；不适用项必须写明理由，不能留空当 PASS。
6. **证据完整**：六阶段时间轴齐全，所有预注册 query 有结果，证据文件 schema 校验通过。

场景阈值必须在首次执行前写进 `scenarios.yaml`。不得在看到结果后放宽；确需改变时创建新场景版本并保留失败证据。

---

## L0 · 冻结教学契约与安全控制面（0.5–1 天）

### 施工内容

1. 建立 `scenarios.yaml` schema，字段至少含：`id`、版本、级别、精确目标、injector、固定强度、TTL、资源上限、前置条件、停止条件、恢复动作、预期症状、证据 query、Runbook 和评分 rubric。
2. 建立 `drills`、`drill_events` 数据模型与 append-only 生命周期；生成不可猜测的 `drill_id`。
3. 实现白名单 Drill Controller：单场景锁、preflight、精确容器标签校验、TTL、`finally` 回滚和幂等 stop；另建独立 Reconciler，从 PostgreSQL active lease 回收失联 Controller 的演练。
4. 实现 `/api/operator/**`、`/api/student/**` 和提交后 reveal token 的独立认证/授权；artifact 与 raw datasource 不走静态公开路径。
5. Drill API 只能 create/get/stop 已登记场景，不接受命令、容器名或任意注入参数；公开 API 无 Docker socket。
6. 为每次演练建立独立证据目录和脱敏 manifest；预留 seed 以便盲测复现。

### Gate L0

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | Catalog 完整性 | 17/17 软件场景均可通过 schema 校验；ID、目标、TTL、停止线和恢复动作无空值 |
| G2 | 权限收口 | 任意未知 ID、篡改目标或额外参数均被拒绝；公开 API 容器无 Docker socket |
| G3 | 单演练锁 | 20 个并发 create 请求只创建 1 个 active drill，其余返回确定性冲突 |
| G4 | 自动回滚 | 对主动异常、超时、重复 stop、注入中 `SIGKILL` Controller、Controller 重启五种路径各测 10 次，50/50 由自身 TTL 或独立 Reconciler 回滚成功 |
| G5 | 资源与隔离 | Learn profile RSS `<4 GiB`；普通数据库 learn 行数为 0；无非 Lab 容器被解析为目标 |
| G6 | 路由授权 | student session 遍历 operator API、artifact path、datasource proxy 和其他 drill 均为 403；reveal token 仅在提交后签发、30 分钟过期且不能执行控制动作；长期 Operator credential 在 bundle/DOM/network/storage/cache 中出现次数=0 |

---

## L1 · 打通统一关联与证据时间轴（1–2 天）

### 施工内容

1. 将不透明 `drill_id` 贯穿 API、RQ meta、worker、dispatcher、sweeper、provider proxy、结构化日志、trace、Langfuse metadata 和异常 Sentry tags；`scenario_id` 只写 Operator control schema/artifact。
2. 用 `drill_events` 构造唯一时间轴，不从 stdout 文本推断状态；事件 sequence 和 phase transition 由数据库约束保护。
3. 实现证据收集器 schema：保存注入见证、指标查询、结构化 stdout 引用、trace/Langfuse/Sentry 引用、恢复和一致性查询；Loki 可检索链接在 L2 接入后填充。
4. 为 Prometheus 使用有界标签和时间窗，不把 `drill_id` 直接做常规 label。
5. 提供 `/api/operator/drills/{id}/timeline|evidence` 和 `/api/student/exams/{id}/timeline|evidence` 两套稳定版本化 schema，并做字段级契约测试。

### Gate L1

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 生命周期正确 | 正常、失败、中止各 10 次；sequence 全部连续、phase 合法、每阶段最多一次 |
| G2 | 关联覆盖 | 测试 drill 的 DB、trace、Langfuse、结构化 stdout 引用及适用的 Sentry 事件 100% 可按不透明 drill_id 定位；本 Gate 不要求 Loki 已接入 |
| G3 | 时间一致性 | 六阶段时间单调；跨来源同一动作的时间差绝对值 ≤5 秒 |
| G4 | 指标基数 | 注入 100 个不同 drill_id 后，Prometheus 不新增 drill-id 级 time series |
| G5 | 证据耐久 | 进程重启后仍可从 PostgreSQL + artifact 重建完全相同的时间轴摘要 |

---

## L2 · 补齐可观测平台（2–3 天）

### 施工内容

1. 用 Alloy 把 API、worker、dispatcher、sweeper、proxy、Drill API、Controller、Reconciler、HAProxy、injector 及采集器自身 stdout/stderr 以结构化 JSON 接入 Loki；明确日志保留、operator/student 分流与脱敏规则。
2. 补全服务和容器指标：
   - API：RPS、成功/错误码、成功与失败延迟分布、active requests；
   - 队列：queue depth、oldest age、outbox pending、dispatch retry；
   - Worker：busy/idle、active lease、lease age、吞吐；
   - Sweeper：存活、扫描时刻、扫描/重入队数量；
   - 容器：up、restart、CPU、memory、I/O、network；
   - 依赖：PostgreSQL/Redis/provider/SSE health、错误率和延迟；
   - 恢复：detect time、protect time、recover time、consistency result。
3. 在 Grafana 新建「Harness Learn Cockpit」：系统拓扑、黄金信号、队列/worker、依赖、容器、恢复时间、告警和 telemetry 自身健康。
4. 为 PostgreSQL、Redis、API replica、Sweeper、资源压力、网络和日志链补告警；对 Firing、Resolved、No Data/Unknown 分别显示，不能把无数据默认为健康。L2 用固定时序回放验证规则，真实注入的 Firing/Resolved 证据分别由 L3/L4 收口。
5. 同一调试台在 Operator 模式提供带时间范围的 Grafana、Tempo、Loki、Langfuse、Sentry 和数据库时间轴跳转；切入盲测模式后只调用白名单 Student Evidence API。

### Gate L2

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 日志接入 | 上述 11 类服务各发送唯一哨兵日志，11/11 在 Loki 中 ≤10 秒可查；适用项字段和 `trace_id` 可解析；Alloy 中无 dropped target |
| G2 | 面板覆盖 | 上述 7 类面板全部有正对照数据；每个面板查询写入自动测试，空查询不算 PASS |
| G3 | 全局拓扑 | 单页显示业务组件，以及 HAProxy、Toxiproxy/netem、Drill API、Controller、Reconciler、Alloy/cAdvisor 的健康/退化/未知状态 |
| G4 | 告警规则回放 | 每条新规则用固定输入序列通过 inactive→pending→firing→resolved 与 no-data/unknown 测试；真实注入证据留给 L3/L4 |
| G5 | Drill 跳转 | 随机抽 10 个 drill，每个适用信号链接均打开正确时间窗和过滤条件，成功率 100% |
| G6 | 遥测自检 | 关闭一条采集链时 Cockpit 明确显示 Unknown/Telemetry gap，不将其显示为服务健康 |

---

## L3 · 补全安全自动级故障（2–3 天）

### 施工内容

1. 将 M3 的 Worker、Dispatcher、Redis、429、Timeout、Load、SSE 七项机制接入统一 catalog/controller/evidence。
2. 正式注册 provider 5xx 场景，验证重试、终态错误分类和告警。
3. 注册坏模型输出场景：缺字段、错误类型、非法 tool arguments、截断 JSON 至少各一个确定性 fixture；验证 schema 闸门在副作用前拒绝。
4. 为九个场景分别固定基线样本、注入强度、恢复 SLO 和一致性 query。
5. 建立单场景和批量 Gate；批量 Gate 每场景使用全新 drill_id，失败即保留证据并停止晋级。

### Gate L3

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 覆盖率 | SA-01～SA-09 共 9/9 场景全部 `ok=true` |
| G2 | 注入真实性 | 9/9 都有机制级正对照；不能仅以“出现错误”证明命中 |
| G3 | 自动恢复 | 9/9 在各自预注册 SLO 内回滚；批量运行结束普通服务全部 healthy |
| G4 | 数据正确性 | 全批次丢 run=0、重复 `tool_call_key`=0、坏输出副作用=0、SSE 缺号/重复=0 |
| G5 | 可观测完整 | 每场景至少 1 黑盒症状 + 1 根因信号 + 1 保护信号 + 1 恢复信号 + 1 一致性结果 |
| G6 | 资源停止线 | 全批次 RSS 峰值 `<4 GiB`，非注入 OOM=0 |
| G7 | 真实告警 | SA 场景映射到的每条告警均有真实 Firing/Resolved；恢复后 5 分钟内残留误报=0 |

---

## L4 · 补全隔离实验级故障（3–4 天）

### 施工内容

1. 通过 Toxiproxy 分别注入 PostgreSQL 断连和延迟；保护普通 DB，验证连接池恢复、超时和事务原子性。
2. 启动 HAProxy 与固定 `learn-api-a/b`，按 §2.3 的幂等流量语义精确杀死一个 replica；从唯一 ingress 观察可用性、错误预算和恢复。
3. 停止 learn sweeper，并配合一个可恢复的过期 lease 制造积压；先证明“不会自行恢复”，再启动 sweeper 验证重扫。
4. CPU/内存场景由 Controller 在精确 learn worker cgroup 内执行固定 `timeout 60s stress-ng` 模板，不启动独立压力 cgroup；CPU 配额固定单核，内存 hard limit 512MiB。I/O 场景在同一 worker 内对 Lab 专用 bind path 的固定 256MiB 文件运行限速 fio，同时由 worker 执行同路径 fsync probe；该路径不与 PostgreSQL/其他项目共享，preflight 要求宿主剩余磁盘 >50GiB。
5. 网络丢包只作用于 learn proxy/netem 容器；保存实际包损/重传/超时见证，恢复后验证连接和数据。

### Gate L4

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 覆盖率 | IX-01～IX-08 共 8/8 场景全部 `ok=true` |
| G2 | DB 原子性 | 两个 DB 场景满足 §2.3 的 30/60 秒强度、样本量和阈值；无半事务、无重复副作用；代理恢复后连接池 ≤60 秒可用 |
| G3 | API 副本容灾 | 600 请求只经 HAProxy；杀死 1/2 replica 后成功率 ≥99%，300 个幂等 POST 恰好 300 runs，30 秒内恢复到 2/2 healthy |
| G4 | Sweeper 诊断性 | 停止期过期 lease 明确积压且无假恢复；恢复后 ≤60 秒完成重扫并进入正确终态 |
| G5 | 压力隔离 | 三种压力均命中目标阈值；非目标容器 OOM/restart=0，宿主剩余内存停止线未触发 |
| G6 | 网络恢复 | 注入窗口的实测 loss 达 manifest 下限；回滚后 30 秒内 loss 回到基线且待处理 run 守恒 |
| G7 | 零逃逸 | 普通数据库、普通 Redis、宿主网络规则和非 Lab 容器前后指纹一致 |
| G8 | 真实告警 | IX 场景映射到的每条告警均有真实 Firing/Resolved；恢复后 5 分钟内残留误报=0 |

---

## L5 · 完成 Runbook 与鉴别诊断库（2–3 天）

### 施工内容

为 17 个软件场景各写一份 Runbook，统一模板：

1. 故障定义、用户影响、风险和适用范围；
2. 注入前基线与必须先确认的 telemetry 健康；
3. 按 Grafana、日志、trace、Langfuse、Sentry、DB/SSE 分列预期现象；
4. 至少两个“长得像但不是它”的混淆故障及排除方法；
5. 系统自动保护动作、它为什么有效、什么时候会失效；
6. 推荐手动恢复顺序和每一步的可执行确认；
7. 禁止动作与可能扩大事故的原因；
8. 恢复 SLO、数据一致性查询和完成条件；
9. 学习问题、证据 rubric 和对应场景版本。

人工监督级另建只读说明，包含识别信号、停止操作、保存证据和人工升级路径，但不含平台可执行注入步骤。

### Gate L5

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | Runbook 覆盖 | 17/17 软件场景都有完整 Runbook，所有章节和 query 非空 |
| G2 | 证据一致 | Runbook 中的每个面板、日志和查询均可由最近一次 PASS artifact 复现 |
| G3 | 鉴别能力 | 每个场景至少 2 个混淆项；随机抽 10 组，关键证据能唯一排除错误候选 |
| G4 | 恢复可执行 | 在隔离运行面逐份执行，17/17 达到预注册恢复和一致性结果 |
| G5 | 安全审查 | 17/17 无任意命令、宿主级破坏、秘密泄漏或普通运行面目标 |

---

## L6 · 在单一调试页内建设渐进课程模块（2–3 天）

### 学习路径

渐进课程按三层呈现同一个真实 drill：

1. **观察课**：告诉故障名称和注入时刻，逐步高亮“症状 → 根因 → 保护 → 恢复 → 一致性”。
2. **引导练习**：只告诉故障大类，学习者自己选面板；按需提供分层提示，并记录用了哪些提示。
3. **独立练习**：隐藏具体故障和高亮，只保留有限候选；提交后逐项对照 Runbook 和真实时间轴。

在现有 `web/index.html` 调试页中增加模式选择器、系统拓扑、当前阶段、统一时间轴、黄金信号、证据抽屉、恢复状态和数据一致性结果。现有 run 输入及中文答案区域原位保留；教学模式只改变同一页面所展示的面板和后端权限，不新增导航入口。

### Gate L6

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 课程覆盖 | 17/17 软件场景都有观察课、引导练习和独立练习配置 |
| G2 | 信息渐隐 | 自动 UI 测试证明三层泄露字段逐级减少；独立练习不显示 scenario_id、注入命令或答案 |
| G3 | 时间轴呈现 | 六阶段及关键证据可在一个页面按时间排序；前端展示与 API artifact 逐项一致 |
| G4 | 拓扑可用 | 故障目标、受影响边和保护组件状态可视；Unknown 与 Healthy 视觉/文本均不同 |
| G5 | 可恢复性 | 浏览器刷新、SSE 重连或前端重启后仍能恢复当前课程状态，不重复触发故障 |
| G6 | 可访问性 | 键盘可完成课程主路径；状态不只靠颜色表达；关键控件有可读标签 |
| G7 | 单前端约束 | 只有现有 `web/index.html`、现有 UI 根地址和一个前端构建；Run/课程/练习模式切换不产生第二页面、端口或独立前端路由 |

---

## L7 · 在同一调试页内建设完全盲测与确定性评分（2–3 天）

### 考试协议

1. 盲测 start 请求不接收 `scenario_id` 或 seed；服务端从已通过 L3/L4 的 allowlist 中生成并保存 seed、随机选题。同一内部 seed、catalog 版本和初始状态必须得到同一场景，但提交前不向浏览器返回二者。
2. 考生在同一调试台切换到盲测模式后，只通过 Student Evidence API 看脱敏后的运行现象、拓扑、指标、日志、trace、模型轮次和 DB 摘要；不获得 raw datasource、artifact 或 operator control 信号，不显示场景 ID、seed、注入进度、Runbook、预期告警或答案。
3. 提交内容固定为：影响范围、故障组件/边界、证据链、建议安全动作、恢复验证、信心分。
4. 评分：根因 40 分、证据 30 分、安全处置 20 分、恢复/一致性验证 10 分；总分 ≥80 通过。选择会扩大故障或越过隔离边界的关键危险动作，整题不通过。
5. 提交前不揭示答案；提交后展示 Runbook、真实时间轴、漏看证据和误判原因。

### Gate L7

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 答案隐藏 | 提交前扫描 DOM、URL、浏览器 network/SSE、Student API、Grafana datasource 请求、Tempo/Loki/Langfuse/Sentry 跳转、本地缓存和所有可下载内容；无 scenario_id、seed、injector、control-plane 信号、Runbook hash 或答案，且无 raw datasource credential/link；student session 枚举 operator/Vault/artifact/raw datasource 路由全部为 403 |
| G2 | 可复现随机 | 100 个 seed 重跑两次，选题与故障参数 100% 一致；无 allowlist 外场景 |
| G3 | 评分确定性 | 同一提交重复评分 100 次结果完全一致；标准满分/错因/危险动作 fixture 全部命中预期 |
| G4 | 证据评分 | 无证据的猜中根因不能超过 50 分；至少两条正确跨信号证据才能获得证据满分 |
| G5 | 考试恢复 | 中途刷新/断线不泄题、不换题、不重复注入；TTL 到期自动回滚并把该题标记 aborted |
| G6 | 盲测演练 | 覆盖 17 个场景各至少 1 次，所有 drill 最终 terminal，自动回滚 17/17 |
| G7 | 模式隔离 | 同一页面切入盲测时先终止 operator fetch/SSE，服务端原子撤销 operator session 后才签发 student session；旧 session 对当前/其他 drill 均为 401/403，迟到响应因 session epoch 不进入页面；退出盲测不创建或跳转到第二前端入口 |

---

## L8 · 全平台验收与教学交付（1–2 天）

### 最终验收批次

1. 在全新 `harness_learn_gate` 数据库、独立 Redis 和新 artifact 根目录执行 17 场景全量 Gate。
2. 运行课程 E2E：每类至少一条观察课、一条引导练习、一条独立练习。
3. 运行盲测 E2E：固定 20 个 seed，并验证评分、揭题、恢复和证据下载。
4. 做全局一致性审计、资源审计、secret 扫描、普通运行面指纹对拍和恢复后健康检查。
5. 把量化结果写入 `docs/EXPERIMENTS.md`，更新 README；只有所有 Gate 为真才标记 Learn Platform 完成。

### Gate L8

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 灾难覆盖 | 17/17 软件故障通过，注入命中率 100%，范围逃逸 0 |
| G2 | 可观测覆盖 | 17/17 有六阶段时间轴及黑盒/根因/保护/恢复/一致性五类证据 |
| G3 | 恢复与正确性 | 自动回滚 17/17；丢 run=0、重复副作用=0、半事务=0、未恢复 active drill=0 |
| G4 | Runbook | 17/17 已用真实 PASS artifact 对照；链接失效 0；人工监督级均标记不可执行 |
| G5 | 单页双模块 | 同一调试页内渐进课程和盲测 E2E 全绿；无第二前端入口；盲测无答案泄漏；固定 seed 可重放 |
| G6 | 资源与安全 | 峰值 RSS `<4 GiB`、非注入 OOM=0、秘密泄漏=0、公开 API Docker socket=0 |
| G7 | 普通运行面 | Gate 前后普通 DB/Redis/容器身份与数据指纹一致；恢复后既有 M1–M3 smoke 全绿 |

---

## 3. 阶段依赖与施工纪律

```text
L0 安全控制面
  └─▶ L1 关联/时间轴
       └─▶ L2 可观测补全
            ├─▶ L3 安全自动级 9 项 ─┐
            └─▶ L4 隔离实验级 8 项 ─┴─▶ L5 Runbook/鉴别库
                       └─▶ L6 渐进课程
                            └─▶ L7 完全盲测
                                 └─▶ L8 全量验收
```

每个阶段固定遵守：

1. 先查官方文档和当前依赖源码/版本，记录检索日期、版本或 SHA；已存在可靠方案就采用，不重复造轮子。
2. 先写失败测试和 Gate 预测，再实现；判据必须放在两个本应相同的对象之间。
3. 一个场景一次只改变一个主变量；组合故障不进入首版 17 项课程。
4. 真实实验只能由固定 wrapper 启动，先核对目标、普通运行面、剩余资源和证据路径。
5. 失败即保存 artifact、执行回滚并停止该 Gate；不得清空失败记录后静默重跑。
6. 每个逻辑阶段独立提交；只暂存明确路径。未经用户再次要求，不自动 push。
7. UI 施工只能在 L0–L5 全部通过后开始；不提前用 mock 页面掩盖故障和观测缺口。

## 4. 主要风险与预案

| 风险 | 预案 |
|---|---|
| Learn 服务使总 RSS 接近 4 GiB | 场景按需启动，不常驻完整副本；达到停止线自动回滚，不调高阈值 |
| Docker 控制能力穿透公开 API | API 与 controller 分进程；只接受 catalog ID；socket 不挂 API；精确 label allowlist |
| Controller 被强杀后 toxic/压力残留 | 注入原语自带 timeout；独立 Reconciler 每 2 秒回收过期 active lease；启动时先做收敛 |
| Prometheus 被 drill_id 撑爆基数 | drill_id 留在 trace/log/DB；metrics 只用有界标签和时间窗/exemplar |
| 计划内故障淹没 Sentry | 受控预期错误作为 metric/log；只有未预期异常和防护失败上报 issue |
| 面板无数据被误判为健康 | telemetry health 独立成状态；No Data/Unknown 与 Healthy 分开 |
| 盲测从前端/API/telemetry 泄露答案 | Student Evidence API 与 Operator Vault 分层；workload 只带不透明 drill_id；提交前扫描全部浏览器可达面 |
| 压力/网络实验影响宿主机 | cgroup/专用卷/代理容器内注入；固定 TTL、硬限额、指纹前后对拍 |
| Runbook 写成静态说明后过期 | 每份 Runbook 绑定 scenario version 和最近 PASS artifact，由 Gate 自动查链接/query |

## 5. 参考基线

- OpenTelemetry 把 traces、metrics、logs、baggage 视为可关联但职责不同的信号；本计划据此保留跨信号 `drill_id`，同时限制 Prometheus 高基数标签：<https://opentelemetry.io/docs/concepts/signals/>
- Google SRE 的监控原则强调 latency、traffic、errors、saturation 四类黄金信号；本计划在此基础上增加队列、依赖、保护和恢复证据：<https://sre.google/sre-book/monitoring-distributed-systems/>
- AWS Fault Injection Service 将实验动作、目标、停止条件和恢复动作显式建模；本计划采用同类安全边界，但只在 Harness Learn 隔离运行面实现：<https://docs.aws.amazon.com/fis/latest/userguide/fis-actions-reference.html>
- Grafana Alloy 官方文档说明其可采集应用和基础设施的 logs、metrics 与 OpenTelemetry 信号；首版据此选 Alloy 1.19 系列接 Docker logs：<https://grafana.com/docs/alloy/latest/>
- cAdvisor 0.60 系列用于读取容器资源指标：<https://github.com/google/cadvisor/releases>
- HAProxy 当前 3.4 是 LTS 分支，首版据此固定 API 双副本入口：<https://www.haproxy.org/>
- Toxiproxy 提供可编程的 TCP disable/latency toxic，首版只用于 learn PostgreSQL 链路：<https://github.com/Shopify/toxiproxy>

检索日期：2026-09-08。具体施工开始前仍需核对所选日志采集、容器指标和网络代理组件的当前官方稳定版本。
