# Harness Lab 独立仓库与 home-5090 迁移实施计划

## 1. 建立可回滚迁移证据

- 记录父仓库状态、Lab 文件清单、Git 忽略检查和服务器硬件/端口基线。
- 查询全部业务表计数、Alembic revision、序列、约束、campaign 关键值；确认没有执行中的 run、
  待处理 outbox 或 Redis job。
- 先生成预迁移备份用于演练；最终切流前停止所有写者，再次核对上述状态并生成最终 custom dump。
- 除 `pg_restore --list` 外，在临时 pristine 数据库执行 `--exit-on-error
  --single-transaction` 完整恢复，验证后丢弃临时库；记录备份 SHA-256。

## 2. 完成 Git 拆分

- 确认父仓库暂存区为空；只向父仓库 `.gitignore` 追加 `/harness-lab/`。由于该文件已有用户的
  `/sandbox-rl-MOPD-lab/` 未提交修改，必须用只作用于 index 的单 hunk patch 暂存 Lab 规则；
  提交前核对 staged diff 的内容精确只有 `/harness-lab/`，不能只核对文件名。
- 在 Lab 增加 `AGENTS.md`、迁移文档和运维文档。
- 在 `harness-lab/` 初始化 `main` 分支，设置目标 GitHub HTTPS remote；先提交并推送一份
  迁移前 Mac 基线，使原 Compose 可复现，再提交服务器改造。
- 用 `git status --ignored` 和大文件扫描确认密钥、模型、数据与 artifacts 未入索引。
- 提交并推送 Lab 初始版本；对父仓库只提交 `.gitignore` 的单独逻辑变更。

## 3. 适配服务器 Compose

- 新增 `ollama` GPU 服务和持久模型 bind mount；GPU reservation 显式 `count: 1`，启动前
  再核对 5090 当前无其他负载。
- 将 API、Grafana 的宿主端口绑定到 `127.0.0.1`；移除 PostgreSQL、Redis、OTLP 和
  Ollama 的外部端口。
- 让服务通过 Compose DNS 使用 `http://ollama:11434/v1`，仍允许环境变量覆盖为 Modal。
- 所有常驻服务配置重启策略；增加健康检查、模型预取入口、服务器 bootstrap/verify 与 SSH
  tunnel 脚本（`ExitOnForwardFailure=yes`、保活、Mac 端只绑定 `127.0.0.1`）。
- 先运行配置渲染测试、单元测试和密钥泄漏扫描。
- `docker compose config` 只运行 `--quiet`；不保存可能展开真实 env_file 的完整渲染结果。

## 4. 一次性服务器引导

- 通过官方 apt repository 安装 Docker Engine、Buildx 和 Compose plugin。
- 将当前用户加入 `docker` group；这等价于授予近似 root 的主机能力，作为本次单用户开发机的
  显式权限决定记录。
- 通过 NVIDIA 官方 repository 安装 `nvidia-container-toolkit`，运行
  `nvidia-ctk runtime configure --runtime=docker` 并重启 Docker。
- 这一步只在交互式 SSH TTY 中执行，由用户本人输入 sudo 密码。
- 验证 `docker run --gpus all ... nvidia-smi`。

## 5. 同步与恢复

- 在 `~/code/projects/Harness-Lab` clone 独立仓库。
- 源端先把 `secrets/.env` 收紧为 0600；目标端先用 `umask 077` 创建 0700 的 secrets 目录，
  再使用 rsync 单写者同步未入 Git 的 `secrets/.env`、模型、逻辑备份、日志和 artifacts；
  显式排除 `.git`、`.venv`、cache、原始 Postgres/Redis/LGTM 数据目录。
- 传输后复核 `secrets/.env` 仍为 0600。
- 启动 pristine PostgreSQL 并把 custom dump 直接恢复进去，不先执行 Alembic；确认现有
  revision 与代码 head 一致后启动 Redis 和其余服务。

## 6. 服务器验收与 Mac 卸载

- 核对所有业务表计数、schema revision、序列、约束、nonterminal/outbox 和关键 campaign 值。
- 宿主测试和 M1 Gate 使用隔离测试数据库/Compose profile，绝不连接正式 `harness`；随后只在
  正式库运行一个带迁移标记的 smoke run，验证模型工具调用、中文最终答案、SSE 重连和可观测
  连通性。
- 模拟 Compose 服务重启并验证 PostgreSQL、Redis、LGTM、API、dispatcher、worker、sweeper、
  Ollama 全部恢复。
- 建立 SSH tunnel 并从 Mac 浏览器访问 API/UI 与 Grafana。
- 服务器验收成功后，精确执行 Lab 的 Mac `docker compose down`；保留所有本地 bind 数据
  作为短期回滚副本，不执行 volume/data 删除。
- 把迁移证据和未完成的 M2 边界更新到 `docs/EXPERIMENTS.md` 与 README，提交并推送。

## 停止条件

- 目标 GitHub 仓库不是空仓库或出现未知历史；
- 父仓库发现已跟踪的 Lab 文件；
- PostgreSQL 存在运行中/等待中的任务，或逻辑备份不可读；
- Docker/NVIDIA 安装需要改变现有驱动或删除冲突包且影响范围不明；
- GPU 容器测试失败；
- 密钥或模型文件将被纳入 Git。

命中停止条件时保留证据，不扩大变更范围。

## 切流后回滚

- 立即停止 5090 的 API、dispatcher、worker、sweeper，确认不再写入。
- 在 5090 生成反向 custom dump，完成可读性检查、临时库试恢复与 SHA-256 记录。
- 停止 Mac 写者，恢复迁移前 Compose 基线并把反向 dump 恢复到 pristine Mac 数据库。
- 只允许一个端点恢复接受请求，验证计数和 smoke run 后再解除回滚状态。
