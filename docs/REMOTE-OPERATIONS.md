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

## 重启与停止

常驻服务使用 `restart: unless-stopped`。主机或 Docker 重启后运行
`scripts/verify_home5090.sh` 复核，不用“容器存在”代替端到端检查。

停止 Lab：

```bash
docker compose down
```

该命令不会删除 bind-mounted 数据。不要使用 `down -v`，也不要删除 `data/`、`models/` 或
`artifacts/`，除非另有明确批准。
