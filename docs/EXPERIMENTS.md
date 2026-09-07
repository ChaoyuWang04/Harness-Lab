# Harness Lab · Experiments

实验记录必须先写预测，再执行。实际结果只能来自保存于 `harness-lab/artifacts/` 的本次证据；旧 Syncopate Serving 结果不能复用。

## M0 · 环境与模型能力

### [M0-G1] Lab-owned Ollama endpoint

- 时间：2026-09-07 10:51–11:49 CST
- 操作：以 `start_ollama.sh` 启动显式使用 Lab 模型目录的 Ollama；对比 PID 文件与 11434 监听者；请求 `/v1/models`
- 预期：实验进程独占 `127.0.0.1:11434`；端点返回 HTTP 200；容器可通过 `host.docker.internal` 访问
- 实际：PID 文件与监听者均为 89883；日志确认 `OLLAMA_MODELS` 指向 `harness-lab/models/ollama`；宿主机端点返回 HTTP 200；一次性 `curlimages/curl:8.12.1` 容器请求 `host.docker.internal:11434/v1/models` 也返回 HTTP 200
- 结论：PASS（宿主机与容器两条路径均通过）

### [M0-G2] Tool-calling success rate

- 时间：2026-09-07 10:55–10:58 CST
- 操作：以固定 prompt、`temperature=0`、`/no_think` 对 `qwen3:0.6b` 采样；严格检查工具名、JSON 参数和 `camp_001` 原样保留
- 预期：至少 14/20 次返回合法 `get_campaign({"campaign_id":"camp_001"})`
- 实际：第一轮 Python urllib 被隐式代理接管，20/20 为 `RemoteDisconnected`，不计模型测量；显式零代理后，原 prompt 20/20 选对工具但把 ID 改成 `001`，严格结果 0/20；仅增加“原样保留 ID”提示后为 20/20 合法调用
- 结论：PASS（20/20 = 100%；同时保留失败轮证据）

### [M0-G3] Docker resource allocation

- 时间：2026-09-07 10:59–11:50 CST
- 操作：读取 Docker daemon 的总内存
- 预期：按用户 2026-09-07 的资源裁定，不少于 7 GiB，且不得为实验擅自提高到 10 GB
- 实际：`docker info` 报告 8,318,976,000 bytes（约 7.75 GiB）；当前 `harness-lab/` 磁盘占用 511 MB，其中 Ollama 模型 498 MB
- 结论：PASS；M2 加入 LGTM 后必须测量容器 RSS、重启与 OOM 状态，若影响宿主机则停下调优，不以增配内存绕过

### [M0-G4] Cloud credential connectivity

- 时间：2026-09-07 11:48 CST
- 操作：从 `harness-lab/secrets/.env` 内部读取配置；对 Langfuse Public API 做只读项目查询；向 Sentry DSN 写入一条标记为 `stage=M0`、`probe=cloud-connectivity` 的测试事件；输出只保留状态码、项目数量和事件 ID
- 预期：两端均返回 HTTP 2xx，且证据文件不包含 DSN、public key 或 secret key
- 实际：Langfuse HTTP 200，项目数 1；Sentry HTTP 200，event_id `146934d65a55444f9bf1c0773ee01f55`；脱敏证据为 `artifacts/m0/cloud_connectivity.json`
- 结论：PASS；这只证明凭据和网络入口可用，SDK 接线仍留到 M2

## M1 · 骨架通车

### [M1-G1/G2] 20-run 端到端基线与创建延迟

- 时间：2026-09-07 12:40–12:43 CST
- 操作：单 worker 连续创建 20 条“诊断 `camp_001` 今日消耗并汇报预算使用率”请求；逐条等待终态，并从 PostgreSQL 核对事件顺序
- 预期：completed ≥18/20；只允许最多 2 条 `BAD_OUTPUT`，其他系统错误为 0；所有 completed run 均包含 created → enqueued → started → tool_call → tool_result → completed；POST p95 <100 ms
- 实际：20/20 completed；`BAD_OUTPUT=0`；系统错误 0；生命周期缺口 0；POST p95 26.542 ms
- 结论：PASS

### [M1-G3/G4] SSE 断线补齐与请求幂等

- 时间：2026-09-07 12:43 CST
- 操作：对一个终态 run 以 `Last-Event-ID: 2` 重连 SSE；同一个 Idempotency-Key 连续 POST 三次并查数据库
- 预期：SSE 只返回数据库中 sequence >2 的连续不重复后缀；三次 POST 只对应一个 run、一行 `agent_runs`
- 实际：完整 sequence 为 1–10，重放为 3–10，无缺号无重复；三次 POST 的唯一 run_id 数为 1，数据库行数为 1
- 结论：PASS

### [M1-G5] Worker 崩溃恢复与副作用恰好一次

- 时间：2026-09-07 12:43–12:44 CST
- 操作：让 attempt=1 在 `adjust_budget(+100)` 的审计事务提交后进入测试暂停，kill worker，立即执行声明的 Compose replacement worker 重启步骤，等待 sweeper 走新 outbox 投递并由 attempt=2 接管
- 预期：≤60 秒 completed；同一 tool_call_key 只有一条 `budget_audit`；预算只从 1000 变到 1100；只有一个终态事件
- 实际：39.801 秒 completed；attempt=2 重放拿到历史工具结果；审计行 1、终态事件 1、最终预算 1100
- 结论：PASS
- 施工发现：RQ 2.12 的 job ID 只允许字母、数字、下划线和短横线，且固定 `job_id=run_id` 会挡住 sweeper 的新恢复投递。最终使用 `{run_id}_outbox_{outbox_id}` 去重同一 delivery，run 级幂等由数据库 `lease_owner + attempt` fencing 保证。

### [M1-G6] Redis 宕机时 Outbox 降级

- 时间：2026-09-07 12:44 CST
- 操作：停止 Redis 后创建 3 条 run，再启动 Redis，并以 30 秒硬截止轮询对应 outbox
- 预期：Redis 停止期间三次 POST 均成功且 run 留在 queued；恢复后 30 秒内三条 outbox 全部 dispatched
- 实际：停止期间成功写入 3/3；恢复后截止前 dispatched 3/3
- 结论：PASS

### [M1-R] 资源与回归

- 资源：Gate 开始时 6 个运行容器合计 RSS 361,632,890 bytes（约 345 MiB），低于 4 GiB；OOM 0；非故障注入 restart 0。Docker Desktop 保持约 7.75 GiB，未增配。
- 回归：最终当前源码在独立 `harness_test` 数据库运行 59 项，58 项通过，只有 1 条 opt-in Ollama 用例跳过；真实 Ollama 已由本次 20-run Gate 覆盖。
- 持久化：正式 Gate 数据保留在 `harness` 数据库；测试清理只作用于 `harness_test`。当前整个 `harness-lab/` 约 753 MB，其中模型 498 MB、数据库目录 72 MB、Lab venv 110 MB。
- 总结：Gate M1 六项全部 PASS；机器证据见 `artifacts/m1/gate_m1.json`。M2 尚未开始。

## M2 · 可观测接入

### [M2-PRE] 预注册

- 时间：2026-09-07 CST
- 范围：修正最终答案的中文渲染；加入 LGTM、OpenTelemetry、Langfuse、Sentry、`/metrics/debug`、四块 Harness SLO 面板和两条告警；不改变 M1 状态机、幂等、lease 或工具事务语义
- 预测：API→dispatcher→RQ workhorse→model/tool 共用一个 W3C trace，run_id 作为跨系统关联字段；30 个 run 后四块面板均有数据；Langfuse 每轮都有 prompt/completion/token；Sentry issue 页面中的指定 event_id 含同一 run_id；停掉 worker 后由 PostgreSQL 持续采样的 oldest-queued-age 告警进入 Firing
- 告警判据：负对照为 worker 正常且无积压 2 分钟保持 Normal；正对照为停掉全部 worker 后 `max(agent_oldest_queued_age_seconds) > 10` 持续 2 分钟进入 Firing，随后立即恢复 worker
- 资源停止线：运行面迁移后在实际 Compose 主机 `home-5090` 测量，记录 hostname、Docker memory 和八项必需服务；LGTM hard limit=`2g`；就绪后和 30-run 后 Lab 合计 RSS 均须 <4 GiB，且 OOM=0、非预期 restart=0。旧 Mac Docker allocation `8,318,976,000` bytes 只保留为 M0 历史证据，不再与 5090 主机做错误的等值比较
- 证据：只接受本轮 `artifacts/m2/` 机器证据；运行结果与主观感受在执行后填写，不预写通过

### [M2-INT] 施工中读数（不代表 Gate 通过）

- UI：SSE payload 现在由浏览器解析 JSON，`run.completed.answer` 进入独立中文结果区；2026-09-07 实际页面提交中文请求后，最终答案以正常中文和 Markdown 文本显示，原始事件区仍保留转义 JSON 并自动换行
- 镜像与端口：`grafana/otel-lgtm:0.32.1` 通过 `dockerproxy.net` 加速下载；arm64 manifest 摘要前缀 `01fcaebef800` 与官方 Docker Hub 标签页一致，随后标记回官方镜像名供 Compose 使用。宿主 `3000` 已被独立 Node 服务占用，Lab 只把 Grafana 宿主端口改为 `3300`，容器内仍为 `3000`，未停止或修改占用者
- LGTM 实机：Grafana 13.2.0、OTel Collector 0.159.0、Prometheus 3.14.0、Tempo 3.0.3、Loki 3.7.7 与 Pyroscope 2.3.0 均启动；`Harness SLO` 四块面板和两条 `for: 2m` 告警规则已由文件 provisioning 真实加载
- 接缝修复：Grafana dashboard provider 必须挂到 provisioning 顶层才能被扫描；OTel 会把带 `unit="ms"` 的指标导出成重复单位后缀，因此 `agent_tool_latency_ms` 使用 dimensionless OTel unit 保持 Prometheus 公共名字为 `agent_tool_latency_ms_*`；Tempo 查询器加入最长 60 秒的最终一致性等待。RQ workhorse 每个进程只有一次累计 metric 落点，不能用 `increase()`；Gate 现以 cohort 起止时刻的 `service_instance_id` 集合差筛出新增 workhorse，再聚合原始 histogram，并硬断言样本数恰好等于 run 数
- 诊断 run：`run_17887635749023dc2174b7a314410` completed；Tempo 单一 trace `2d2f7a65aae4a8b58d8b71cf587b6e97` 同时包含 `harness.api_create_run`、`harness.dispatch`、`harness.execute_run`、`harness.model_call`、`harness.tool_call`；Langfuse 返回 2 轮完整 generation，token 分别为 466/46/512 与 324/432/756
- 告警正负对照：worker 正常且队列年龄 0 时连续观察至少 120 秒无活动告警；停 worker 后专用 run `run_1788764218091257b92aa739b4abe` 的 oldest queued age 升至 147.313 秒并进入 Firing；脚本立即恢复 worker，run completed 且活动告警归零。证据见 `artifacts/m2/alert_verified.json`
- 面板尺子正对照：3 个非正式诊断 run 全部 completed，新增 workhorse metric instance 恰好 3 个；快照聚合得到 run P95 4.75 秒、failed rate 0、queue lag P95 23.875 秒、model/tool 非空系列 3。该批次只验证查询算法，不代替最终 30-run Gate
- 回归：重建后 `scripts/verify_m1.py` 六项全部 PASS；最新完整集成回归为 93 passed / 1 skipped（隔离 `harness_test` 数据库）
- 云端连接：Langfuse SDK `auth_check=True`；Sentry 受控 probe 已获得 event_id `53781a9083f04fc3ae5f9d9e681c4ea8`，关联 `run_id=run_sentry_m2_90524`，脱敏证据见 `artifacts/m2/sentry_probe.json`。这只证明 SDK 写入/认证，不替代 G3/G4 的页面和字段验收
- 资源：迁移前 Mac 读数为启动 LGTM 前约 359.65 MiB、全栈约 1,552.54 MiB。迁移后 `home-5090` Docker memory 为 33,237,381,120 bytes；修复复验时八项服务齐全，合计约 1,255 MiB，低于 4 GiB 停止线；OOM=0、restart=0。最终值仍由唯一 30-run Gate 重测
- Sentry 页面证据：已在用户打开的 Edge 中核对 issue `SYNCOPATE-2`；页面同时显示 probe 的完整 event ID `53781a9083f04fc3ae5f9d9e681c4ea8` 与 `run_id=run_sentry_m2_90524`。截图及结构化记录为 `artifacts/m2/sentry_issue_53781a90.jpg`、`sentry_issue_verified.json`；最终 Gate 会再次硬校验它们与 `sentry_probe.json` 身份一致
- Gate 验收器加固：0.6B 模型可能直接回答而不调用工具，因此不再错误地固定检查 cohort 第一条 run；现在只在同一唯一 cohort 内寻找一条同时具备完整 Tempo span 和 Langfuse generation 的 run
- Dashboard 实测修正：用户从 UI 短时间提交约 10 个请求后，PostgreSQL 显示端到端耗时从 2.316 秒升至 36.732 秒，原始累计 histogram 的 queue-lag P95 为 45.9375 秒、run execution P95 为 4.75 秒，但原面板 `rate(...[$__rate_interval])` 返回 `NaN`。根因是每个 RQ workhorse 只导出一个累计样本便退出，单样本序列无法计算 `rate()`。面板与 failed-rate 告警已改为聚合 Collector 保留的累计值，并明确其“当前 telemetry stack 生命周期”口径；零失败回退为 0%，模型错误计数与工具延迟拆分左右单位轴。重建 LGTM 后以 10 个带 `M2-DASHBOARD-REPAIR` 标记的并发 run 复验：10/10 terminal、run execution P95=4.75 秒、queue-lag P95=45.8333 秒、failed rate=0%、model 200=10，证明真实队列压力可被读取；这批仅是修复诊断，不代替最终 30-run cohort。

### [M2-GATE] 唯一 30-run 正式验收

- 时间：2026-09-07 22:06–22:08 CST
- 操作：在 `home-5090` 无其他 producer、queued/running/outbox 均为 0 的前提下，通过固定入口 `scripts/run_verify_m2_home5090.sh` 创建唯一一批 30 条真实 run；从该 cohort 的新增 workhorse metric instance、Tempo、Langfuse、已核对的 Sentry issue 和真实告警正负对照统一验收
- 结果：30/30 completed、唯一 run 30、failed rate=0%；run execution P95=4.75 秒，queue lag P95=175 秒，model/tool 非空系列=3。单 worker 在突发 30 请求下的主要瓶颈是排队，不是单条执行时间；175 秒超过 10 秒 SLO 线，是被面板正确揭示的容量基线，不影响“指标可读”Gate 判据
- Trace/Langfuse：run `run_1788790010265c7dc0eea66b142bb` 的单一 Tempo trace `ccb56b61a2413c53aec388dafd2554cf` 覆盖 API、dispatcher、worker、model 和 tool；Langfuse 同一 run 有 2 轮完整 generation，token 分别为 466/75/541 与 324/342/666
- Sentry/告警：Sentry issue `SYNCOPATE-2` 的 event ID 与 run ID 和发送 probe 一致；queue-age 告警正对照进入 Firing，恢复 worker 后活动告警归零
- 资源：实际运行主机 `samwang-X870I-AORUS-PRO-ICE` 的八项服务齐全；合计 RSS 2,139.458 MiB <4 GiB，OOM=0、unexpected restart=0
- 结论：G1–G5 全部 PASS，`all_passed=true`；M2 COMPLETE。脱敏机器证据见 `artifacts/m2/gate_m2.json`

### [M2-SUBJECTIVE] 人工感受（用户本轮实际反馈，≤5 行）

- 用户在前端连续提交约 10 条请求时，肉眼看到返回明显变慢，却发现原 P95 面板没有变化；这直接暴露了监控虽已接线、查询口径却不正确。修正后同类压力和正式 30-run cohort 都能显示真实 queue lag，面板现在能区分“模型执行慢”和“排队慢”，比只看日志/数据库更快定位瓶颈。

## M3 · 故障注入与压测

### [M3-PRE] 预注册与运行边界

- 时间：2026-09-07 CST；用户已明确批准执行完整 M3，并要求 Gate 完成后再设计前端一键故障演示
- 范围：严格执行 EXP-1 worker 猝死、EXP-2 dispatcher 双写窗口崩溃、EXP-3 Redis 宕机 60 秒、EXP-4 429/timeout provider 退化、EXP-5 500×50 单/四 worker 压测、EXP-6 20 客户端各五次 SSE 重连；阈值不在执行后调整
- 隔离：破坏性实验只写独立 `harness_m3` 数据库与 Redis DB 1；正常 `harness` 数据库不清理、不写 M3 run。失败保留证据，不自动重跑冒充首次结果
- 注入口：Chaos Proxy 使用固定 seed 的确定性计数序列；dispatcher 使用落盘 one-shot marker；worker/Redis 只按 Compose 解析出的精确容器身份操作。每个实验后局部恢复，整个 Gate 结束或异常时由 wrapper trap 恢复普通单 worker、直接 Ollama、正常数据库
- 判据与实现计划：`docs/plans/2026-09-07-m3-chaos-design.md`、`docs/plans/2026-09-07-m3-chaos-plan.md`。实际结果只从 `artifacts/m3/` 回填

### [M3-GATE-1] EXP-1 前置契约失败并停止

- 时间：2026-09-08 00:31–00:34 CST
- 操作：进入 EXP-1，依次提交 15 个真实 `adjust_budget(+1)` 候选；只有工具事务已提交后才允许 kill worker。此前两次启动分别在基础镜像解析和首个实验 run 前的 Compose 宿主路径检查停止，`harness_m3` 均为 0 run，不计实验测量
- 实际：15/15 都选择了 `adjust_budget`，但全部以 `TOOL_ERROR` 结束，`budget_audit=0`，因此实际 worker kill 注入为 0/10，按预注册判据立即停止，EXP-2～6 未执行。相同模型和合成输入的只读探针返回 `{"campaign_id":"001","delta":1}`，确认 0.6B 模型删掉了 `camp_` 前缀；M3 脚本没有复用 M0 已验证的完整精确标识符约束句
- 结论：FAIL / STOP。它证明本轮输入契约不满足注入前置条件，不构成 worker 恢复机制失败。失败数据库和 `artifacts/m3/exp_1.json`、`failure.json` 保留；普通单 worker、Redis、dispatcher、sweeper 和正常 `harness` 数据库已由 trap 恢复
- 修复：M3 所有工具臂已改为明确要求 `campaign_id` 是精确字符串 `camp_001` 且不得省略前缀；阈值、样本数和故障方式不变。若执行新的正式 Gate，必须使用新的隔离数据库/Redis namespace 和独立证据目录，不能覆盖本轮失败

### [M3-GATE-2-PRE] 修复后独立复验

- 时间：2026-09-08 CST；用户已明确批准按推荐方案重新完整执行
- 隔离：使用新 PostgreSQL 数据库 `harness_m3_gate2`、Redis DB 2 和 `artifacts/m3/gate2/`；Gate 1 的数据库与证据保持不变
- 输入前置：创建任何实验 run 前，以零故障 proxy 对相同模型执行 3 次只读工具参数探针；只有 3/3 都精确返回 `adjust_budget`、`campaign_id=camp_001`、`delta=1` 才进入 EXP-1
- 判据：EXP-1～6 的样本数、故障比例、时限、延迟、成功率、资源停止线和最终全局 Gate 与 `[M3-PRE]` 完全相同，不因 Gate 1 失败调整
- 停止：前置探针或任一注册实验失败即保留 Gate 2 证据并恢复普通运行面，不继续后续实验，也不覆盖 Gate 1

### [M3-GATE-2] EXP-1 通过，EXP-2 镜像接线失败并停止

- 时间：2026-09-08 CST。首次启动在任何工具探针和实验 run 前发现 verifier 仍硬编码 Redis DB 1；修正为只允许非零隔离 DB 后，`harness_m3_gate2` 仍为 0 run，再进入实际 Gate 2
- EXP-1：3/3 工具参数前置通过；真实 worker crash 10/10 completed，全部由 attempt 2 接管，最大恢复时间 44.255 秒；十次 `budget_audit` 均恰好一条。结论 PASS
- EXP-2：run 最终 completed，started/terminal 各一条且 outbox 最终 dispatched，但 `dispatcher_exit_code=null`、未观察到 crash 后 pending 窗口，结论 FAIL；按停止线未执行 EXP-3～6
- 根因：M3 wrapper 只构建了 `api` 和 `chaos-proxy` 镜像。Compose 为 `dispatcher`、`worker`、`sweeper`、`migrate` 使用各自镜像名，因此 dispatcher 仍是 M2 旧镜像，新 crash hook 没有进入真实被测进程
- 修复：wrapper 改为构建全部 Python 服务镜像，并在创建数据库/run 前从 `harness-lab-dispatcher` 镜像导入 `crash_after_publish_once` 作为接线正对照。Gate 2 数据和 `artifacts/m3/gate2/` 保留；新的全量 Gate 必须继续使用新 namespace

### [M3-GATE-3-PRE] 全 Python 镜像接线后的独立复验

- 时间：2026-09-08 CST；沿用用户“按推荐方案完整完成 M3”的授权
- 隔离：新 PostgreSQL 数据库 `harness_m3_gate3`、Redis DB 3 和 `artifacts/m3/gate3/`；Gate 1/2 均不删除、不覆盖
- 新正对照：wrapper 在任何 run 前构建 api/dispatcher/worker/sweeper/migrate/chaos-proxy 全部镜像，并要求 dispatcher 镜像能导入一次性 crash hook；随后仍须通过 3/3 精确工具参数探针
- 判据与停止线：与 `[M3-PRE]` 完全一致。任一项失败即保留 Gate 3 并停止，不把前两次通过的局部结果拼接成总体通过

### [M3-GATE-3] EXP-2 收敛观测竞态并停止

- 时间：2026-09-08 CST
- EXP-1：本轮独立 10/10 completed，最大恢复时间 44.360 秒，十次审计均恰好一条，PASS
- EXP-2：dispatcher 真实以 code 91 退出，崩溃窗口 outbox 为 pending，run 最终 completed，started/terminal 各一条；但验收器在恢复 dispatcher 后立即读取 outbox，先于其下一次 1 秒轮询，读到最终状态仍为 pending，因而 FAIL 并停止 EXP-3～6
- 根因与修复：这是验收器的收敛竞态，不是注入缺失。最终判据仍要求 outbox=`dispatched`，但允许在恢复后轮询最多 10 秒；超时仍按最后读数失败。Gate 3 数据和 `artifacts/m3/gate3/` 保留

### [M3-GATE-4-PRE] Dispatcher 收敛修复后的全量复验

- 时间：2026-09-08 CST；沿用用户要求完整完成 M3 的授权
- 隔离：新 PostgreSQL 数据库 `harness_m3_gate4`、Redis DB 4 和 `artifacts/m3/gate4/`；前三轮证据均不覆盖
- 新回归：dispatcher 恢复后最多等待 10 秒收敛到 dispatched；此等待只消除采样竞态，不改变退出码 91、crash 后 pending、started/terminal 各一条等原判据
- 其他判据、输入前置、镜像正对照、资源停止线和失败即停规则与 `[M3-PRE]` 完全一致

### [M3-GATE-4] 跨 Gate marker 权限前置失败并停止

- 时间：2026-09-08 CST
- EXP-1：本轮独立 10/10 completed，最大恢复时间 44.429 秒，十次审计均恰好一条，PASS
- EXP-2：注入前删除 one-shot marker 时触发 `PermissionError`，没有创建 EXP-2 run；Gate 3 marker 由 root dispatcher 容器以 mode 0600 创建，而非 root verifier 无权复位它。按停止线未执行 EXP-2～6
- 修复：每个 Gate 在数据库和实验 run 创建前，由一次性 root 容器只挂载 `data/chaos/` 并精确删除 `dispatcher-after-publish.once`；不递归清理、不碰其他证据。Gate 4 数据和 `artifacts/m3/gate4/` 保留

### [M3-GATE-5-PRE] 精确 marker 复位后的全量复验

- 时间：2026-09-08 CST；沿用用户要求完整完成 M3 的授权
- 隔离：新 PostgreSQL 数据库 `harness_m3_gate5`、Redis DB 5 和 `artifacts/m3/gate5/`；Gate 1～4 均不删除、不覆盖
- 新正对照：全 Python 镜像构建与 hook import 后，精确复位单个 dispatcher marker，再执行 3/3 工具参数探针；所有动作都发生在首个实验 run 前
- 判据与停止线：继续完全沿用 `[M3-PRE]`，不拼接前轮局部 PASS

### [M3-GATE-5] EXP-3 单 worker 恢复容量不足并停止

- 时间：2026-09-08 CST
- EXP-1：本轮独立 10/10 completed，全部由 attempt 2 接管，最大恢复时间 44.361 秒，十次审计均恰好一条，PASS
- EXP-2：dispatcher 真实以 code 91 退出，崩溃窗口 outbox 为 pending；run 最终 completed，started/terminal 各一条，outbox 最终 dispatched，PASS
- EXP-3：Redis 恢复后 120 秒截止时仅 48/60 条进入终态，FAIL 并停止 EXP-4～6。恢复普通运行面前的数据库复核为 completed=50、queued=10；60/60 Outbox 均已 dispatched，因此失败位于消费容量，不是 Outbox 丢失或恢复投递失败
- 根因：验收器在恢复 Redis 后额外固定为单 worker；已完成的 50 条终态跨度约 131.5 秒，单 worker 的实测完成速率约 22.8 run/min，不足以在原定 120 秒内消化 60 条。原计划没有规定恢复阶段必须保持单 worker，并已在 EXP-5 明确要求验证四 worker 扩缩容
- 处理：不延长 120 秒、不减少 60 条、不覆盖 Gate 5。恢复策略改为 Redis 就绪后立即扩至四 worker，Gate 结束仍由 trap 恢复普通单 worker；用新的 Gate 6 独立复验全部六项

### [M3-GATE-6-PRE] 四 worker 有界恢复策略的全量复验

- 时间：2026-09-08 CST；沿用用户要求按推荐方案完整完成 M3 的授权
- 隔离：新 PostgreSQL 数据库 `harness_m3_gate6`、Redis DB 6 和 `artifacts/m3/gate6/`；Gate 1～5 均不删除、不覆盖
- EXP-3 策略：Redis 宕机期间仍严格创建 60 条且不启动消费；Redis 恢复后扩至四 worker，仍要求 120 秒内 60/60 终态、Outbox pending=0、零丢失。普通运行面最终恢复为单 worker
- 其他样本数、故障比例、时限、输入前置、镜像/marker 正对照、资源停止线与失败即停规则均与 `[M3-PRE]` 相同

### [M3-GATE-6] EXP-4 告警判据被验收器错误收紧并停止

- 时间：2026-09-08 CST
- EXP-1：10/10 completed，最大恢复 44.428 秒，审计均恰好一条，PASS。EXP-2：dispatcher code 91，pending 窗口、唯一 started/terminal、最终 dispatched 全部成立，PASS
- EXP-3：60/60 POST 成功，宕机期间 queued=60、pending Outbox=60；Redis 恢复后四 worker 在 39.819 秒内完成 60/60，最终 pending=0，PASS
- EXP-4：正常臂 30/30；429 臂 29/30 completed、44 次 retry、45 个注入 429，唯一失败为 `MODEL_429`；timeout 臂 29/30 completed、33 次 retry、34 个注入 timeout，唯一失败为 `MODEL_TIMEOUT`。timeout 臂告警 Firing，零故障恢复后 Resolved；429 臂未 Firing，验收器因此判 FAIL 并停止 EXP-5/6
- 根因：文件计划只要求“退化期间”queue alert 进入 Firing，验收器却额外要求 429 和 timeout 两臂分别都 Firing。429 臂 queue-lag P95=122.837 秒，但 backlog 在 `>10 秒持续 2 分钟` 加 10 秒评估对齐完成前清空；把及时消化也判失败，会错误鼓励降低恢复速度
- 处理：恢复原注册语义——至少一个真实退化臂触发 queue alert；两臂仍分别要求 30 条、固定注入比例、retry>0、completed≥70%、注入计数>0、错误码纯净，恢复后仍须 Resolved。不改任何数值阈值、样本量或告警规则；Gate 6 全部证据保留

### [M3-GATE-7-PRE] 退化期间告警语义修正后的全量复验

- 时间：2026-09-08 CST；沿用用户要求按推荐方案完整完成 M3 的授权
- 隔离：新 PostgreSQL 数据库 `harness_m3_gate7`、Redis DB 7 和 `artifacts/m3/gate7/`；Gate 1～6 均不删除、不覆盖
- EXP-4 判据：429 与 timeout 两臂各自仍须完成所有原机制判据；两臂中至少一个使现有 queue alert 真实 Firing，恢复后必须 Resolved。该修正移除的是验收器未注册的额外 AND，不改变 Grafana 的 `>10s for 2m` 规则
- EXP-1～3、5～6 和全局 Gate 的样本数、时限、资源停止线、正对照、失败即停规则继续沿用 `[M3-PRE]`

### [M3-GATE-7] EXP-5 API 创建容量不足并停止

- 时间：2026-09-08 CST。EXP-1～3 全部通过；EXP-4 正常臂 30/30，429 与 timeout 臂均 29/30 completed、各仅一个预期错误码，timeout 告警 Firing、恢复后 Resolved，整体 PASS
- EXP-5 单 worker 臂 500/500 completed、0 failed，POST P95=359.674 ms、queue-lag P95=1448.182 秒、吞吐 19.696 run/min；四 worker 臂 500/500 completed、0 failed，POST P95=541.326 ms、queue-lag P95=376.065 秒、吞吐 75.888 run/min
- 结论：worker 1→4 将 queue lag 降低约 74.0%，执行吞吐提高约 3.85 倍，相关门槛通过；但两个臂的 POST P95 均超过 150 ms，EXP-5 FAIL 并停止，EXP-6 未执行
- 根因证据：两个 500-run 创建批次的数据库创建时间跨度分别为 2.503 秒和 3.073 秒；当前单 Uvicorn API 进程约 160～200 req/s，50 并发在入口形成数百毫秒排队。失败不是模型执行或 worker 扩容无效
- 处理：API 状态只在 PostgreSQL，入口可安全多进程化；将同一 API 容器改为四个 Uvicorn worker。先用专用空数据库跑固定 500×50 API-only 诊断并检查 150 ms 与 4 GiB 停止线；诊断未通过则不启动新的全量 Gate

### [M3-API4-DIAG-1] 四 API 进程的首次隔离诊断

- 时间：2026-09-08 CST；专用数据库 `harness_m3_api4_probe`，500×50 创建完成后不由 dispatcher 消费，普通 API 由 trap 恢复
- 结果：诊断按 150 ms 门槛返回 FAIL，因此没有启动 Gate 8；但失败路径先抛断言、后写证据，精确 P95 未落盘，只保留数据库和 `artifacts/m3/diagnostics/failure.json`
- 处理：不猜测数值、不复用数据库。先修正诊断器，使 PASS/FAIL 都先落 `created`、concurrency、POST P95、创建 wall time 和 run-ID digest，再用 `harness_m3_api4_probe2` 独立复测

### [M3-API4-DIAG-2] 四 API 进程的数值化隔离诊断

- 时间：2026-09-08 CST；专用数据库 `harness_m3_api4_probe2`，固定 500×50 API-only 负载
- 实际：500/500 创建，wall time=1.433 秒，但 POST P95=504.556 ms，FAIL；四个 Uvicorn 子进程均真实存在。API 容器 RSS=510.5 MiB，已贴近 512 MiB limit；全 Lab 同时约 2.1 GiB，仍低于 4 GiB 停止线
- 分析：四进程已把平均吞吐提高到约 349 req/s，但尾延迟仍高。当前自写并发器为每个 POST 新建 urllib opener/HTTP 连接，不符合原计划 `hey` 的连接复用行为；先把这一差异作为单变量验证，不归因于 PostgreSQL
- 下一诊断：批量创建的每个 executor thread 复用独立 `requests.Session` 且禁用宿主代理继承；四进程 API limit 调到 768 MiB 留出运行余量，但总实际 RSS 仍须 <4 GiB。用全新 `harness_m3_api4_probe3` 复测相同 500×50 和 150 ms 门槛

### [M3-API4-DIAG-3] 连接复用后的隔离诊断

- 时间：2026-09-08 CST；专用数据库 `harness_m3_api4_probe3`，固定 500×50 API-only 负载，每个 executor thread 复用 HTTP session
- 实际：500/500 创建，wall time=0.796 秒，数据库创建跨度=0.738 秒，POST P95=157.861 ms；相比 DIAG-2 的 504.556 ms 大幅下降，但仍超过 150 ms，FAIL。API RSS=556.4 MiB / 768 MiB，资源余量正常
- 分析：连接复用假设成立，但当前压测仍比原计划的 `hey` 多执行逐请求唯一 Idempotency-Key：每条都额外获取 PostgreSQL advisory lock 并写幂等表；`hey -H` 只能整轮固定 header，原注入命令不会产生这种路径
- 最后一项单因素诊断：仅 EXP-5/API probe 省略 Idempotency-Key，使请求路径与 `hey` 对齐；M1 幂等 Gate 及 M3 provider/SSE 批次继续使用唯一 key。用全新 `harness_m3_api4_probe4` 测相同 500×50、连接复用和 150 ms；若仍失败，停止微调并重新审查 API/数据库架构

## 运行面迁移 · 独立 Git + home-5090

### [MIG-G1] Git 边界

- 时间：2026-09-07 15:49–16:08 CST
- 操作：确认父仓库未跟踪任何 Lab 文件；在 Lab 初始化独立 `main`，先提交 Mac 可回滚基线，再提交 5090 部署；父仓库以 index-only 单 hunk 暂存 Lab 忽略规则
- 预期：独立仓库不含密钥、模型、数据库、日志、cache 或 artifacts；父仓库提交不夹带已有 `/sandbox-rl-MOPD-lab/` 用户改动
- 实际：独立基线 `02d8d1a`、5090 部署 `0ce190f` 已推送到 `ChaoyuWang04/Harness-Lab`；父仓库只含 `/harness-lab/` 的 `2085f1b` 已推送；用户原改动保持未暂存
- 结论：PASS

### [MIG-G2] Docker、GPU 与模型

- 时间：2026-09-07 16:14–16:36 CST
- 操作：使用 Docker 与 NVIDIA 官方 apt repository 安装 Docker Engine/Compose/NVIDIA Container Toolkit；比较官方 Docker Hub 与加速源 OCI digest 后拉取 Ollama；启动 GPU Compose 服务
- 预期：容器可见一张 RTX 5090；`qwen3:0.6b` 使用 Lab 自有模型目录；Ollama、PostgreSQL、Redis、LGTM 仅在 Compose 网络内，API/Grafana 只绑定服务器 loopback
- 实际：Docker 29.8.0、Compose 5.5.1、NVIDIA Container Toolkit 1.20.0；Ollama 0.33.3 官方与加速源 index digest 均为 `sha256:32931b46719f673c05fdbaa81ccb26da18ea4a1c57590a754874ab28ba269eb2`；容器报告 RTX 5090 32,607 MiB，模型 522 MB；端口验收通过
- 结论：PASS

### [MIG-G3] PostgreSQL 一致性与切流

- 时间：2026-09-07 16:22–16:42 CST
- 操作：先在 Mac 临时空库、再在 5090 临时空库对 custom dump 做 `--exit-on-error --single-transaction` 完整恢复；确认 nonterminal/outbox/Redis queue 为 0 后停止 Mac 写者，生成最终 dump 并恢复到 5090 pristine 数据库
- 预期：最终 dump 传输前后 SHA-256 相同；全部业务表计数、revision、campaign 值、约束数和三个序列值逐项一致；全程只有一个写端
- 实际：最终 SHA-256 `a8e0b0bd0f402ef33736ec1e5385829d96d09a074bf83b7493d03cb60d74188f`；切流前 40 runs、404 events、nonterminal=0、pending outbox=0；七张业务表、`0001_m1_schema`、campaign 值、15 个约束及序列 354/356/16 全部相等；恢复后再启动 5090 API
- 结论：PASS

### [MIG-G4] 端到端、重启和卸载 Mac 运行面

- 时间：2026-09-07 16:39–16:47 CST
- 操作：运行两个带 migration 标记的真实 Agent smoke run；运行原 M0 直接工具调用采样；从 5090 验证 Sentry/Langfuse；重启八个常驻服务；经 SSH tunnel 从 Mac 请求 API/Grafana；最后关闭 Mac Compose 和本地 Ollama
- 预期：run 进入终态、SSE 中文可解码；直接工具调用 ≥14/20；Sentry/Langfuse 均 2xx；重启后全栈恢复；Mac 本地无 Harness 容器和 11434 listener，隧道仍可访问 5090
- 实际：两个 run 均 completed，SSE 正常中文；但 0.6B 在完整 Agent prompt 下两次都跳过工具，其中一条答案无依据，记质量 WARN。原注册直接工具请求 20/20 合法；Sentry 200、Langfuse 200；重启后八个常驻服务和 GPU/model 验收通过；Mac Harness 容器为 0、11434 无监听，隧道 API/Grafana 均成功
- 结论：PASS（迁移/运行面）；模型工具选择 WARN 不等于修复。该条是迁移当时的状态，当前 M2 最终结论以上方 `[M2-GATE]` 为准
- 证据：`artifacts/migration/home5090-migration-summary.json`、`tool-calling-home5090-20.json`、`cloud-connectivity-home5090.json` 以及两份数据库 dump
