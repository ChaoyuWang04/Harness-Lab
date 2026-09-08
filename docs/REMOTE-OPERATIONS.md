# home-5090 运行手册

Harness Lab 的常驻服务运行在 `home-5090`。Mac 只负责编辑、Git、SSH 控制和浏览器访问。

## 技术栈边界

- 默认模型后端：Compose 内的 Ollama `0.33.3`，使用一张 RTX 5090，模型为 `qwen3:0.6b`。
- API 仍只依赖 OpenAI-compatible `/v1`。需要大模型时，在服务器的 `secrets/.env` 覆盖
  `HARNESS_COMPOSE_LLM_BASE_URL` 和 `LLM_MODEL` 即可接 Modal；不要为此改 Agent 代码。
- 当前低并发实验不引入 vLLM。只有开始测连续批处理、多 Worker 或吞吐上限时才单独评估。
- PostgreSQL 是事实源，Redis 只是队列。LGTM、Sentry 和 Langfuse 保持 M2 的可观测边界。

## 首次安装

`scripts/bootstrap_home5090.sh` 完整采用 Docker 与 NVIDIA 的官方 apt repository。它会安装
Docker Engine、Compose plugin 和 NVIDIA Container Toolkit 1.20.0，并把调用 sudo 的用户加入
`docker` group。Docker group 具有近似 root 的主机权限；这是这台单用户开发机的显式决定。

必须由用户本人在交互式 SSH 中输入 sudo 密码：

```bash
sudo ./scripts/bootstrap_home5090.sh
```

脚本遇到现有冲突包会停止，不会自动卸载。安装后退出 SSH 并重新连接，使 group 生效。

## 启动

```bash
docker compose --env-file secrets/.env config --quiet
docker compose up -d postgres redis lgtm ollama
docker compose exec ollama ollama pull qwen3:0.6b
docker compose up -d
./scripts/verify_home5090.sh
```

所有宿主端口只绑定服务器 loopback。PostgreSQL、Redis、OTLP 和 Ollama 没有宿主端口。

## Mac 访问

在 Mac 的 Lab 目录运行：

```bash
./scripts/home5090_tunnel.sh
```

随后访问：

- Harness UI：`http://127.0.0.1:18000`
- Grafana：`http://127.0.0.1:13300`

## 数据迁移与验证

- custom dump 必须恢复到 pristine 空数据库，不先运行 Alembic。
- 传输前后核对 SHA-256；正式恢复前先在临时空库完整试恢复。
- `verify_m1.py` 会清表，只能连接隔离测试数据库，禁止对正式 `harness` 执行。
- 切流前停止所有写者并确认没有 nonterminal run、pending outbox 或 Redis job。
- 切流后回滚必须先停止 5090 写者并生成反向逻辑备份，不能让 Mac 与 5090 双写。

## M2 Gate

M2 的资源读数必须来自实际 Compose 主机，不能在 Mac 上运行 verifier。服务器不安装项目
Python 环境；固定入口复用现有 app image，并只为 verifier 挂载源码、Docker socket 和只读
Docker CLI：

```bash
./scripts/run_verify_m2_home5090.sh
```

该入口会在 host network 中访问只绑定 loopback 的 API/Grafana，并以当前用户身份写
`artifacts/m2/gate_m2.json`。运行前必须完成 Sentry issue 页面证据并确认没有其他 run producer；
它只允许创建计划中登记的唯一 30-run cohort。

## M4 数据回流 Gate

M4 只在 `home-5090` 运行。Mac 负责在 `main` 上按命名路径提交并推送；服务器 checkout
必须保持 clean，只执行 `git pull --ff-only`，不得从服务器提交。开跑前：

```bash
# Mac
git status --short
git diff --check
git push origin main

# home-5090
cd /home/samwang/code/projects/Harness-Lab
git status --short
git pull --ff-only
./scripts/run_verify_m4_home5090.sh new gate1
```

`new` 的预期首次终态是退出码 3 和 `PENDING_HUMAN`，不是故障。50 条 raw 轨迹只保存在：

```text
/home/samwang/code/projects/Harness-Lab/artifacts/m4/gate1/
```

M4 生成与 replay 期间会暂时停止本地 LGTM，并只对该隔离运行面关闭 OTLP 导出；PostgreSQL
model turns、Langfuse 和 Sentry 仍保留。此时 Grafana 链接短暂不可用是预期行为。退出 trap 会先
重新启动 LGTM，再用 `HARNESS_OTEL_EXPORT_ENABLED=true` 恢复普通服务；M2/M3 的观测配置不变。

只允许把下面两个已经脱敏的文件复制到 Mac 同名 ignored 目录，并核对
`review_sample.json` 内登记的 SHA：

```text
artifacts/m4/gate1/attribution/review_sample.json
artifacts/m4/gate1/attribution/human_review.json
```

人工完成 15 条判定并保持 `sample_sha256` 不变后，将 `human_review.json` 传回原目录，再运行：

```bash
./scripts/run_verify_m4_home5090.sh resume gate1
```

若 SSH 断线、shell 被强制结束或主机重启，先恢复普通运行面：

```bash
./scripts/run_verify_m4_home5090.sh --restore-only gate1
```

最终 PASS 后只把 `eval/dataset_v1.jsonl`、`eval/dataset_v1.manifest.json` 和可选脱敏报告
复制回 Mac，并逐个核对 remote manifest SHA。`raw/`、数据库、Redis 状态、控制 token 和
运行时日志不得离开 `home-5090`，也不得进入 Git。API/UI 与 Grafana 仍使用既有 SSH tunnel；
M4 proxy、PostgreSQL、Redis 和 Ollama 没有宿主端口。

## 重启与停止

常驻服务使用 `restart: unless-stopped`。主机或 Docker 重启后运行
`scripts/verify_home5090.sh` 复核，不用“容器存在”代替端到端检查。

停止 Lab：

```bash
docker compose down
```

该命令不会删除 bind-mounted 数据。不要使用 `down -v`，也不要删除 `data/`、`models/` 或
`artifacts/`，除非另有明确批准。
