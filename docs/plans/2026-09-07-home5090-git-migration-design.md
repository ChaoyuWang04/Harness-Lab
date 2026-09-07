# Harness Lab 独立仓库与 home-5090 迁移设计

## 目标

把 `harness-lab/` 从父项目的 Git 边界中完全隔离，并把 Lab 的常驻运行负载迁到
`home-5090`。Mac 只保留代码编辑、Git 操作和 SSH 控制，不再承载 PostgreSQL、Redis、
LGTM、API、Worker 或模型推理。

## 已确认现场

- 目标仓库 `ChaoyuWang04/Harness-Lab` 已创建，是可访问的空仓库。
- `harness-lab/` 当前没有被父仓库跟踪，也没有自己的 `.git`。
- Lab 约 1.2 GB；主要空间来自 Ollama 模型、PostgreSQL/LGTM 数据、缓存和虚拟环境。
- `home-5090` 是 Ubuntu 24.04，RTX 5090 约 32 GB VRAM，磁盘余量约 719 GB。
- 服务器当前没有 Docker、Compose、NVIDIA Container Toolkit 或 Ollama。
- Sentry 与 Langfuse 密钥已经存在于 Lab 的 `secrets/.env`，该文件继续禁止进入 Git。
- M0、M1 已通过；M2 施工已接通主要链路，但尚未完成 Sentry UI 证据和唯一 30-run cohort，
  因而本次迁移不能写成 M2 Gate 已通过。

## 技术栈决定

### 模型服务

RTX 5090 不要求使用 Ollama。Harness 只要求一个 OpenAI-compatible `/v1` 端点，理论上可接
Ollama、vLLM 或 Modal。

当前默认继续使用 Ollama：

- 现有 M0/M1 已在 `qwen3:0.6b` + Ollama 上验证，迁移风险最低；
- 单 Worker、小模型实验不需要 vLLM 的高并发调度能力；
- Ollama 官方支持 RTX 50 系列，并支持 Linux NVIDIA GPU 容器；
- 模型服务与其余栈同由 Compose 管理，故障恢复和数据目录边界更直观。

保留 `LLM_BASE_URL` 和 `LLM_MODEL` 配置面，后续较大模型可切换到 Modal，不要求修改
Agent、队列或 API 代码。只有当实验进入多 Worker、高吞吐或连续批处理测量时，才单独评估
vLLM；不在这次迁移中引入第二套本地 serving runtime。

### 服务部署

采用 Docker Engine + Compose plugin + NVIDIA Container Toolkit 的服务器原生安装。所有 Lab
服务由一份 Compose 管理，Ollama 通过 GPU device reservation 使用 RTX 5090。此方案需要用户
在服务器首次安装时输入一次 sudo 密码；密码不进入脚本、聊天、日志或仓库。

所有端口绑定到服务器 `127.0.0.1`，不直接暴露到 LAN 或公网。Mac 使用 SSH 本地转发访问：

- API/UI：Mac `localhost:18000` → 服务器 `127.0.0.1:8000`
- Grafana：Mac `localhost:13300` → 服务器 `127.0.0.1:3300`

PostgreSQL、Redis、OTLP 和 Ollama 不对 Mac 暴露。

## Git 边界

- 父仓库 `.gitignore` 增加锚定规则 `/harness-lab/`。
- `harness-lab/.git` 是独立仓库，远端为
  `https://github.com/ChaoyuWang04/Harness-Lab.git`。
- Git 跟踪代码、测试、Compose、配置模板、固定脚本和文档。
- Git 不跟踪 `secrets/.env`、`.venv`、模型、数据库、缓存、日志和原始实验产物。
- 新增 Lab 自己的 `AGENTS.md`，让未来进入独立仓库的 agent 只按 Lab 文档和 Gate 工作。

## 状态迁移

运行态不通过 Git 搬运：

1. 先确认没有活跃 run、待处理 outbox 或 Redis job，再停止 API、dispatcher、worker、
   sweeper，建立明确停写窗口；停写后再次核对并用 `pg_dump` 生成最终 PostgreSQL 逻辑备份；
2. 同步代码、模型 blobs、日志和实验 artifacts 到服务器；
3. 不复制 `.venv`、Python cache、Redis 临时队列、LGTM 内部存储或运行中的 PostgreSQL
   数据目录；
4. custom dump 包含 schema、数据、约束与 `alembic_version`，因此直接恢复到 pristine 空库，
   不先运行 Alembic；恢复后核对 `alembic current` 与代码 head 一致；
5. Redis 作为传送带从空状态启动，PostgreSQL 继续作为唯一事实源；
6. LGTM 从空状态启动，历史 Gate 证据由 `artifacts/` 保留，迁移后重新形成服务器观测基线。

## 验收标准

- 父仓库 `git status` 不再列出 `harness-lab/`。
- 独立仓库能从 GitHub 全新 clone，且不存在密钥和大文件。
- 服务器 `docker compose config` 通过，所有公开端口仅绑定 `127.0.0.1`。
- GPU 容器能运行 `nvidia-smi`，Ollama 能识别 RTX 5090，`qwen3:0.6b` 可调用。
- PostgreSQL 备份传输前后 SHA-256 一致，并先在临时空库以 `--exit-on-error
  --single-transaction` 完整试恢复；正式恢复后全部业务表计数、Alembic revision、序列、约束、
  nonterminal/outbox 状态及 campaign 关键值与源端一致。
- `pytest` 与 M1 验证只使用隔离测试库；不得对恢复后的 `harness` 运行会清表的 verifier。
  隔离验证后再在正式库创建一个明确标记的迁移 smoke run，验证 API、SSE 中文答案与队列终态。
- Grafana、Sentry 与 Langfuse 最小连通性通过；不把连通性写成 M2 Gate 完成。
- Mac Harness 容器关闭后，Mac 仍可通过 SSH 隧道使用 UI 和 Grafana。

## 回滚

- 在服务器验收完成前不删除 Mac bind-mounted 数据。独立仓库先提交一份迁移前 Mac Compose
  基线，确保旧拓扑可复现。
- 切流前可直接恢复 Mac 基线。切流后若 5090 已接受新写入，则必须先停止 5090 所有写者、
  生成并验证反向逻辑备份，再恢复到 Mac；禁止让两端同时接受写入。
- 切流后保留一个明确的回滚观察期，只有用户另行确认才删除 Mac 数据副本。
- Git 拆分不依赖删除历史：父仓库此前没有跟踪 Lab，只需移除其忽略规则即可回到原状态。

## 权限裁定

把用户加入 Docker group 会赋予近似 root 的主机控制能力。本实验选择该常见的单用户开发机方案，
但把它作为显式安全决定记录；若用户不接受，则需改成每次使用 sudo 或另做 rootless 环境准备，
不在本迁移中静默切换。
