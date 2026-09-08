# Harness Control Plane · 身份、策略与资源治理施工计划书

> 目标：在 Harness M4 和 Harness Learn L0–L8 完成以后，为真实多用户、多模型、多工具与多沙盒运行建立一个最小但可信的产品控制面。它负责回答：**谁，以什么身份，在什么租户中，经过什么授权，能够调用哪个版本的 Agent、模型、工具和沙盒；本次 run 获得多少预算；高风险动作由谁批准；发生撤权或故障后如何停止、恢复和审计。**
>
> 本文是未来 Control Plane 的唯一阶段计划，阶段编号使用 **C0 → C9**。它不改变 [Harness M0–M4](harness-lab-plan.md) 或 [Harness Learn L0–L8](learn-platform-plan.md) 的排期和通过条件，也不授权现在提前施工。

---

## 0. 开工条件、边界与完成定义

### 0.1 硬前置

Control Plane 的正式 C0 Gate 只能在以下证据全部成立后通过：

1. Harness M4 的七个 Gate 在同一新鲜隔离运行面全部通过，`docs/EXPERIMENTS.md` 有唯一结果和证据链接；
2. Harness Learn L0–L8 全部通过，17 个软件故障场景、Runbook、统一时间轴、课程和盲测入口已经验收；
3. 普通 Harness 运行面已恢复，M1–M3 回归无新增失败；
4. `home-5090` 的资源余量、已有服务、端口和独立 Lab 边界重新实测；
5. Agent Framework Lab 完成 F8-G3 接口包 Gate，能够提供版本化 `AgentSpec`、`GraphRunRequest` 和 `GraphRunResult`；
6. Sandbox Lab 至少提供版本化的 `SandboxProfile`、`SandboxLease`、`ToolCall`、`ToolResult` 与回收语义。

第 1–4 项是开始任何 C0 工作的硬前置。第 5–6 项缺失时，只允许在 C0 中整理消费者需求、威胁模型和候选 Schema；C0 状态必须保持 `PENDING`，不得执行通过声明或进入 C1。真实接口和 contract fixture 到位后，必须替换候选字段并重新运行 C0 全部 Gate；不凭设计文档想象集成成功。

依赖关系固定为：

```text
Harness M4 → Learn L0–L8 → Control Plane C0–C9
                         ↘ Agent Framework 接口
                         ↘ Sandbox Lab 接口
```

### 0.2 两种“控制面”必须分开

| 名称 | 负责 | 不负责 |
|---|---|---|
| Learn Drill Controller | 只接受白名单故障场景 ID，注入、TTL 回滚、证据收集 | 用户身份、业务授权、模型路由、真实第三方凭据 |
| 产品 Control Plane | Tenant、Principal、Connection、Grant、Registry、Quota、Policy、Approval、Sandbox Broker | 直接执行任意 Docker 命令、注入故障、替代 Harness 队列 |

二者可以共享审计和观测基础设施，但不得共享公开路由、长期凭据、数据库角色或 Docker 权限。Learn 的 operator 身份不能自动成为产品管理员。

### 0.3 控制面与运行面的边界

```text
Control Plane（低频配置与决策）
  ├── Identity / Tenant / Principal
  ├── Connection / Secret reference
  ├── Agent / Model / Tool / Sandbox Registry
  ├── Grant / Approval / Quota / Routing Policy
  └── Policy Compiler ──生成不可变 RunPolicy──┐
                                                ▼
Data / Run Plane（高频执行）              Harness run + outbox
                                                │
                                      worker / Agent Framework
                                        │               │
                                   Model Gateway    Tool Gateway
                                        │               │
                                      models      Sandbox Broker
```

关键原则：

1. Control Plane 不进入每一个 token 的热路径；run 创建时编译并冻结 `RunPolicy`，运行中只消费其不可变快照。
2. 模型永远看不到 OAuth refresh token、API key 或数据库连接；模型只看到无秘密的 `connection_id` 和允许调用的工具 Schema。
3. Tool Gateway 才能解析 `connection_id`、校验 Grant/Approval、取得短期凭据并执行动作。
4. PostgreSQL 继续是真相源；Redis 仍只是传送带；Control Plane 不另造一套 run 状态机。
5. Policy、Registry 和 Connection 更新只影响未来 run；已经冻结的 run 若必须强制停止，走显式 revocation/fencing，不静默改写快照。

### 0.4 “全部完成”的定义

只有同时满足以下条件才能标记 Control Plane 完成：

- 两个 Tenant、每个至少两个 Principal、两个外部 Provider 的隔离矩阵全部通过；
- 未授权调用拒绝率 100%，跨租户可见性和数据泄漏为 0；
- 模型输入、SSE、普通 API、日志、trace、Langfuse、Sentry 和 artifact 中秘密泄漏为 0；
- 每个 run 都能还原精确的 identity、policy、model、tool、connection 与 sandbox 版本；
- 审批前真实写副作用为 0，批准后在重试/恢复下仍恰好一次；
- Connection 撤销、Grant 撤销、配额耗尽和沙盒 lease 过期均在预注册时间内生效；
- Control Plane 失联时，已接受 run 按冻结策略有界运行，新 run fail closed；
- Harness、Framework、Gateway、Sandbox 和第三方 API 的统一追踪与恢复证据完整；
- 形成回归主线所需的接口版本、迁移、回滚和所有权清单，但不在本计划中直接合并主线。

### 0.5 本计划刻意不做

- 不做通用云管理平台、Kubernetes 控制器或任意容器编排 API；
- 不让浏览器或模型直接持有第三方长期凭据；
- 不为追求“多平台”一次接入大量 SaaS；先证明两类身份和两种 Provider 契约；
- 不把 Agent Framework 的思考图、Harness 的可靠执行、Sandbox 的隔离执行搬进 Control Plane；
- 不在 Control Plane 中训练、微调、蒸馏或选择模型能力；这里只登记版本和运行策略；
- 不把 code existence、表存在或配置登记当成真实接线通过。

---

## 1. 核心对象与不可变契约

### 1.1 最小对象模型

| 对象 | 最小内容 | 所有者 |
|---|---|---|
| `Tenant` | tenant_id、状态、默认配额 | Control Plane |
| `Principal` | user/service 身份、tenant_id、状态 | Control Plane |
| `Connection` | provider、owner、secret_ref、scopes、状态 | Control Plane |
| `Grant` | subject、resource、actions、conditions、版本 | Control Plane |
| `AgentSpec` | graph/version、输入输出 Schema、能力声明 | Framework Registry |
| `ModelSpec` | provider/model/revision、能力、成本元数据 | Model Registry |
| `ToolSpec` | Schema、风险级、幂等语义、所需 scope | Tool Registry |
| `SandboxProfile` | provider、资源、网络、TTL、镜像/环境版本 | Sandbox Registry |
| `Approval` | 提案 hash、批准人、范围、过期时间 | Control Plane |
| `RunPolicy` | 以上对象解析后的不可变执行快照和 hash | Policy Compiler |
| `CapabilityReceipt` | 实际消费的版本、调用、证据和结果摘要 | Harness/Data Plane |

### 1.2 `RunPolicy` 必须冻结的字段

- `tenant_id`、`principal_id`、`actor_type`；
- `agent_spec_id@version`、允许的 graph entrypoint；
- 候选模型、路由顺序、最大模型调用次数、token/费用上限；
- 允许的工具及每项 action、risk、approval requirement；
- 可用 connection 的不透明 ID 与 scope 摘要，不含秘密；
- sandbox profile、最大并发、TTL、网络策略；
- run 截止时间、重试预算、取消与 revocation epoch；
- telemetry/privacy policy；
- `policy_version`、规范化 JSON SHA256、创建时间。

同一规范化输入必须产生逐字节相同的 policy JSON 和 hash。任何动态信息必须显式列为运行读数，不能偷偷改变 hash 语义。

### 1.3 统一关联键

```text
tenant_id → principal_id → run_id → graph_thread_id
                               ├→ trace_id
                               ├→ policy_id / policy_hash
                               ├→ approval_id
                               ├→ connection_id
                               ├→ sandbox_lease_id
                               └→ tool_call_id / idempotency_key
```

高基数 ID 进入日志、trace 和数据库，不直接作为 Prometheus 常驻标签；Grafana 通过时间窗、exemplar 和 drill/run 页面跳转。

---

## C0 · 冻结职责、威胁模型和接口（1–2 天）

### 施工内容

1. 写清浏览器、API client、Control Plane、Harness、Framework、Model Gateway、Tool Gateway、Sandbox Broker 和第三方 Provider 的信任边界。
2. 冻结上述对象的 Pydantic/JSON Schema、错误分类和版本兼容规则。
3. 建立调用矩阵：谁能读取/写入什么，谁可以解析 secret_ref，谁能创建沙盒。
4. 为未来表、服务和 API 预定目录，但不在本阶段实现业务逻辑。
5. 给每个接口建立 contract fixture 与正负样例；所有 fixture 使用假身份和假秘密。
6. 登记外部依赖的官方版本、认证模式、限流和撤权语义；未选真实 Provider 时保留接口，不猜字段。

### Gate C0

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 对象完整 | 10 类核心对象均有版本化 Schema、owner、读者和写者 |
| G2 | 权限闭合 | 信任边界图中不存在“模型/浏览器直接读取长期秘密”的路径 |
| G3 | 契约可执行 | 每个跨组件 Schema 至少 1 个正例、3 个负例，contract tests 全绿 |
| G4 | 依赖真实 | Framework/Sandbox 对接字段来自实际版本证据；未知字段明确 PENDING |
| G5 | 无提前施工 | M4/Learn 证据链接完整；未修改其 Gate，未创建真实凭据或第三方资源 |

---

## C1 · Tenant、Principal 与身份传播（1–2 天）

### 施工内容

1. 增加 tenants、principals、memberships、service_accounts 和 session/audit 表迁移。
2. 建立两种 actor：交互用户与后台 service account；禁止共享身份。
3. API 验证身份后只向下游传播签名的最小 identity envelope，不传播浏览器 session。
4. PostgreSQL 查询强制 tenant predicate，并增加跨租户负向测试。
5. 为停用用户、停用 tenant、过期 session 和重放 token 建立 fail-closed 行为。

### Gate C1

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 身份矩阵 | 2 tenants × 2 users × 2 service accounts 的 8 个主体均可独立识别 |
| G2 | 隔离 | 读、写、列表、SSE、错误信息各 100 次跨租户尝试，泄漏/成功次数均为 0 |
| G3 | 传播 | 100/100 run 的 API、DB、queue、worker、trace identity 一致 |
| G4 | 撤销 | principal 停用后新请求立即 401/403，已排队未开始 run 在 5 秒内 fenced |
| G5 | 审计 | 每次身份判定有 actor、tenant、decision、reason、policy version，不记录 token |

---

## C2 · Connection 与秘密隔离（2–3 天）

### 施工内容

1. 建立 connection metadata 与 secret reference 分离存储；仓库、DB 普通列和事件中不保存明文长期秘密。
2. 定义 API key 与 OAuth connection 两种 adapter；先用本地假 Provider 完成 contract。
3. Tool Gateway 按调用临时解析 secret_ref；返回给 Framework 的只有 scope、健康和安全错误。
4. 实现 token refresh 单飞锁、过期处理、撤销和 rotation。
5. 对日志、trace、Langfuse、Sentry、SSE、错误响应和 artifact 做 sentinel 扫描。

### Gate C2

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 秘密边界 | repo、DB dump、API、SSE、日志、trace、Langfuse、Sentry、artifact 扫描泄漏=0 |
| G2 | Scope | 允许 scope 20/20 成功；缺少 scope 20/20 在调用 Provider 前拒绝 |
| G3 | Refresh | 50 个并发过期请求只触发 1 次 refresh，其他请求复用新 token |
| G4 | Rotation | rotation 前后各 20 次调用成功，旧版本从切换后 5 秒内不可用 |
| G5 | Revocation | connection 撤销后下一次调用 100% fail closed，不回退到其他用户连接 |

---

## C3 · Agent、Model、Tool 与 Sandbox Registry（1–2 天）

### 施工内容

1. 建立四类 Registry；所有条目不可原地改语义，只能发布新版本或停用。
2. AgentSpec 引用 Framework graph；ToolSpec 声明风险、scope、输入输出和幂等语义；ModelSpec 声明结构化输出/工具调用能力；SandboxProfile 引用 Sandbox Lab 契约。
3. Policy Compiler 只解析状态为 active 且兼容的精确版本。
4. run 完成后写 Capability Receipt，记录实际使用而非仅记录配置候选。

### Gate C3

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 版本不可变 | 已发布对象修改语义 20/20 被拒；发布新版本 20/20 成功 |
| G2 | 精确解析 | 模糊名称、缺版本、已停用、不兼容各 20 个请求全部拒绝，不自动猜测 |
| G3 | 接线 | 每类对象都有真实消费者判据；删除 fixture 后相应正向测试必红 |
| G4 | Receipt | 50/50 run 可还原实际模型、工具、连接、策略和沙盒版本 |

---

## C4 · Grant、风险分级与人工审批（2–3 天）

### 施工内容

1. 冻结 `read / propose / write / destructive` 四级动作；ToolSpec 每个 action 必须标级。
2. Grant 使用默认拒绝、显式允许；先实现可解释的 RBAC + 条件约束，不让 LLM 决定权限。
3. Framework 只生成 `ActionProposal`；Control Plane 对规范化 proposal 计算 hash。
4. 高风险动作暂停在独立 approval node；批准绑定 proposal hash、actor、范围和过期时间。
5. Tool Gateway 在执行前再次校验 policy、grant、approval、revocation epoch 和 canonical `SHA256(run_id + graph_thread_id + action_index + proposal_hash + tool_spec_version)` 幂等键。

### Gate C4

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 授权矩阵 | 4 风险级 × allow/deny × user/service account 共 16 个组合，每组合重复 10 次；160/160 决策与预期一致 |
| G2 | 审批隔离 | 审批前和拒绝后真实副作用=0；批准后副作用恰好一次 |
| G3 | 防篡改 | proposal 任一字段变化后，旧 approval 100/100 失效 |
| G4 | 恢复 | approval 前后各注入 worker crash 20 次，状态可恢复且无重复副作用 |
| G5 | 可解释 | 每个 deny 都返回稳定 reason code；不向调用方泄漏其他主体或策略内容 |

---

## C5 · 配额、费用与模型路由策略（2–3 天）

### 施工内容

1. 定义 tenant/principal/run 三层预算：并发、run 数、模型调用、token、估算费用、工具次数、沙盒时间。
2. Model Gateway 只消费冻结的候选模型与升级条件；记录选择理由、fallback 和实际用量。
3. 配额预留与结算写入 PostgreSQL；worker 重试不重复扣除已经结算的调用。
4. 控制面不可用时，新 run 不获得临时无限配额；已接受 run 只能用冻结余量。
5. 建立软告警和硬停止线，不通过提高预算让 Gate 变绿。

### Gate C5

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 并发 | 预设 1/2/4 并发上限各压 100 请求，实际占用从不超过上限 |
| G2 | 计量守恒 | 100 个含重试 run 的 provider receipt 与本地结算逐项一致，重复扣费=0 |
| G3 | 硬停止 | token、费用、工具和沙盒四类预算耗尽各 20 次，均在下一动作前停止 |
| G4 | 路由可解释 | 100% 模型调用记录候选集、选择、原因、latency、usage 和 fallback |
| G5 | 失联 | Control Plane 停止 60 秒：新 run 100% fail closed，已接受 run 不越过冻结预算 |

---

## C6 · Sandbox Broker 集成（2–3 天）

### 施工内容

1. 通过版本化 adapter 消费 Sandbox Lab 的 profile/lease/execute/terminate 接口，不复制其 provider 逻辑。
2. 每个 lease 绑定 tenant、run、policy、profile、TTL 和 fencing token。
3. Broker 负责创建、续租、回收和 reconcile；Tool Gateway 只能对当前 lease 执行已授权工具。
4. 禁止宿主 HOME、其他 Lab 路径、Docker socket 和业务秘密默认挂载。
5. 对 provider timeout、进程崩溃、网络断连和孤儿 lease 建立有界恢复。

### Gate C6

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 契约一致 | fake provider 与至少一个真实 sandbox provider 的 contract suite 100% 一致 |
| G2 | 隔离 | 两租户互访 filesystem/process/network/lease 共 200 次，成功和泄漏=0 |
| G3 | 回收 | 正常、取消、TTL、worker crash、broker crash 各 20 次，100/100 最终回收 |
| G4 | Fencing | 旧 lease/token 在续租代际变化后 100/100 不可再执行 |
| G5 | 宿主边界 | 挂载和网络清单逐项扫描，不出现禁用路径或未授权出口 |

---

## C7 · 第一条真实第三方 API 链路（2–3 天）

### 施工内容

1. 选择一个低风险、可创建测试资源并可彻底清理的 Provider；优先任务/日历类，不以生产账户开局。
2. 先做只读工具，再做需要审批的单一写工具；所有对象带 Lab 前缀和清理身份。
3. 使用两个 Tenant 的独立测试账户或独立授权容器，禁止共用 Connection。
4. 形成 baseline → authorize → read → propose → approve → write → verify → revoke → cleanup 时间轴。
5. 真实 API 的费用、配额、权限和清理规则在运行前登记。

### Gate C7

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 只读 | 两 Tenant 各 20 次读取只返回自身测试数据；跨租户数据=0 |
| G2 | 写入 | 每 Tenant 10 次批准写入，Provider 侧结果恰好一次；拒绝 10 次写入=0 |
| G3 | 撤权 | 撤销 OAuth/Connection 后 20/20 后续调用在 Provider 动作前失败 |
| G4 | 恢复 | 429、timeout、5xx 各 20 次，状态守恒且无重复创建 |
| G5 | 清理 | 所有 Lab 测试对象按精确 ID 删除并由独立列表查询确认残留=0 |

---

## C8 · 第二 Provider、跨渠道与策略差异（2–3 天）

“平台”在本计划中拆成两个维度：

- **外部 Provider**：Google、Microsoft、任务系统等被调用平台；
- **接入 Channel**：Web、CLI、API 等用户进入系统的渠道。

本阶段接入第二 Provider，并以 Web + API 两个 Channel 证明身份和权限来自 Control Plane，而不是前端硬编码。

### Gate C8

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | Provider 矩阵 | 2 tenants × 2 users × 2 providers 的允许/拒绝矩阵 100% 符合预期 |
| G2 | Channel 等价 | Web/API 对同一 identity+policy 的标准化决策和副作用逐字节等价 |
| G3 | 错配防护 | provider、connection、tenant 错配各 50 次全部在外部调用前拒绝 |
| G4 | 独立撤权 | 撤销 Provider A 不影响 B；撤销 user A 不影响同 tenant user B |
| G5 | 观测 | 每次真实调用从 UI/DB/trace 可定位到 policy、approval、connection 和 receipt |

---

## C9 · 全链故障验收与主线融合准备（2–3 天）

### 施工内容

1. 建立 Control Plane 专用回归和故障矩阵：DB/Redis/API/worker/gateway/broker/provider/refresh/approval/revocation。
2. 复用 Learn 的观测方法，但不把 Learn 注入权限开放给产品身份。
3. 输出跨 Lab 接口包：Schema、版本兼容、fixture、contract tests、owner、SLO、Runbook。
4. 输出回归主线的 staged migration：先 gateway/contract，再 identity/policy，再真实 provider；每步可回滚。
5. 明确重复模块取舍，以证据选择保留实现，不按目录整体拷贝。

### Gate C9

| # | 指标 | 通过标准 |
|---|---|---|
| G1 | 端到端 | 预注册 100-run cohort 终态守恒，丢 run=0、重复副作用=0、跨租户泄漏=0 |
| G2 | 故障恢复 | 每个适用故障都有检测、保护、恢复、一致性证据；自动恢复成功率 100% |
| G3 | 可追溯 | 100/100 run 可从最终结果反查全部 Capability Receipt 和策略链 |
| G4 | 安全回归 | secret、授权、approval、sandbox、channel 全矩阵无高危失败 |
| G5 | 融合包 | 所有跨 Lab 接口有版本、fixture、contract test、owner、回滚方案，断链=0 |
| G6 | 现有能力 | Harness M1–M4、Learn L0–L8 适用回归全部保持通过 |

---

## 2. 预定目录与证据布局

```text
harness-lab/
├── app/control_plane/              # identity、registry、policy、approval、quota
├── app/gateways/                   # model/tool adapter；不放 provider secret
├── app/sandbox_broker/             # 仅跨 Lab 契约 adapter
├── config/control_plane/           # versioned policy/catalog fixtures
├── docs/control-plane-plan.md       # 本文，唯一阶段计划
├── docs/control-plane/              # Runbook、Schema 导航、融合包
├── tests/control_plane/             # unit/contract/security/integration
├── scripts/verify_control_plane_*.py
└── artifacts/control-plane/<gate_id>/  # 原始证据，不提交秘密
```

表可以与 Harness 共用 PostgreSQL 实例，但必须使用独立 schema/role；run 侧只能读取编译后的 policy 和安全 projection，不能读取 secret reference 的解析材料。

## 3. 证据与状态规则

1. 每阶段先在 `docs/EXPERIMENTS.md` 登记预测、命令、停止线、资源和 artifact 路径。
2. Gate 只接受实际运行读数；配置登记、mock-only 或单元测试不能冒充真实 Provider/Sandbox 通过。
3. 每个 Gate 结果写 `PASS / WARN / PENDING / REMOVED`；安全缺口不能降级成 WARN 后晋级。
4. artifact 至少包含 normalized config、schema versions、policy hashes、测试结果、资源读数、清理与恢复证据。
5. 每阶段一个 scoped commit；只暂存精确路径。是否 push 仍由届时用户授权决定。
6. 真实凭据只进受控 secrets；文档、测试 fixture、截图和日志全部使用假值或脱敏引用。

## 4. 与其他文档的分工

- [Harness 计划](harness-lab-plan.md)：可靠接收、执行、恢复和数据回流；不负责多租户产品治理。
- [Learn 计划](learn-platform-plan.md)：故障注入、观察和教学；不负责真实业务授权。
- [Sandbox Lab 计划](../../sandbox-rl-MOPD-lab/docs/sandbox-rl-lab-plan.md)：环境隔离、工具执行、RL/MOPD 实验；Control Plane 只消费版本化接口。
- [Agent Framework Lab 计划](../../agent-framework-lab/docs/agent-framework-lab-plan.md)：目标理解、图编排、模型协作和验收；不持有长期凭据。
- [总体架构指南](../../docs/agentic-system-architecture-guide.md)：解释能力应该落在哪一层以及何时路由/训练/蒸馏；不记录本计划进度。

## 5. 主要风险与预案

| 风险 | 预案 |
|---|---|
| Control Plane 进入 token 热路径导致整体脆弱 | run admission 时冻结 policy；运行只消费本地快照与 revocation epoch |
| LangGraph checkpoint 与 Harness retry 重复副作用 | approval 与 execute 分 node；共享 canonical key=`SHA256(run_id + graph_thread_id + action_index + proposal_hash + tool_spec_version)`，Tool Gateway 为最终执行者 |
| Registry 看似存在但真实调用未消费 | 每类对象设置正对照与删除后必红的接线测试 |
| OAuth refresh 风暴 | connection 级单飞锁、版本 fencing、有限重试 |
| 两个 Lab 的相似对象语义漂移 | versioned Schema + fixture + 双方 contract test，不复制实现 |
| 测试账户污染真实账户 | 独立测试租户、Lab 前缀、精确清理清单、Provider 侧终态复核 |
| 可观测系统泄漏敏感信息 | 默认字段 allowlist、sentinel 全渠道扫描、异常上报前 scrub |
| “未来融合”演变成大爆炸重写 | C9 只交 staged migration；一次集成一个契约并保留回滚路径 |
