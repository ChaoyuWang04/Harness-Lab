# Harness Lab

Harness Lab 是一个与 Syncopate 主线实现和 Git 历史隔离的 Serving Harness 实验。

## 边界

- 独立仓库为 `ChaoyuWang04/Harness-Lab`；父项目通过 `/harness-lab/` 规则忽略本目录。
- 不复用父项目的模型、Python 环境、Runtime、数据库、队列或审计目录。
- Lab 自有的代码、文档、配置模板、模型、数据、日志、缓存和实验产物全部保存在本目录内。
- 常驻服务运行在 `home-5090`；Mac 只负责编辑、Git、SSH 控制和浏览器访问。
- 第三方程序本体以及 Docker 自身管理的镜像层不属于 Lab 产物；Lab 服务的持久数据必须使用本目录下的 bind mount。
- 密钥只写入 `secrets/.env`，不得进入 Git、日志或实验记录。
- 后续 Compose 必须显式使用 `env_file: ./secrets/.env`，不得改回根目录 `.env`。
- 服务器端口只绑定 loopback，通过 SSH tunnel 从 Mac 访问；数据库、队列、OTLP 和模型端口不对宿主网络发布。
- 默认推理后端是 RTX 5090 上的容器化 Ollama；更大模型可通过配置切换到 Modal 的 OpenAI-compatible 端点。

## 当前阶段

M0 与 M1 已通过。M2 已获人工批准并进入施工：中文最终答案渲染、OpenTelemetry、Langfuse、Sentry、LGTM、四块 Grafana 面板、指标、追踪传播和真实告警正负对照均已接通。M2 仍未通过 Gate：Sentry 已成功写入指定 event，但还缺 issue 页面中该 event 与 `run_id` 的可见证据；因此唯一 30-run cohort 尚未启动。当前正在把运行面迁到 `home-5090`，迁移成功不等同于 M2 Gate 通过。

服务器安装、启动、隧道、数据迁移和回滚见 `docs/REMOTE-OPERATIONS.md`。

## 目录

- `config/`：可提交的无密钥配置模板
- `docs/`：实施计划和实验账本
- `scripts/`：Lab 固定入口与验收程序
- `tests/`：Lab 自己的测试
- `secrets/`：本地密钥文件，不提交
- `models/`：Ollama 模型，不提交
- `logs/`、`cache/`、`artifacts/`：运行时及验收产物，不提交
