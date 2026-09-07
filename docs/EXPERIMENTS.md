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
