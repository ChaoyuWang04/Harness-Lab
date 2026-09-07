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

M0、M1、M2 已通过。运行面位于 `home-5090`，Mac Harness 容器与本地 Ollama 已停止；Mac 通过 `127.0.0.1:18000`（API/UI）和 `127.0.0.1:13300`（Grafana）的 SSH tunnel 访问。M2 已完成中文最终答案渲染、OpenTelemetry、Langfuse、Sentry、LGTM、四块 Grafana 面板、指标、跨进程追踪和真实告警正负对照；唯一 30-run Gate 为 30/30 completed，五项验收全部通过。具体基线和证据见 `docs/EXPERIMENTS.md`。M3 尚未开始。

服务器安装、启动、隧道、数据迁移和回滚见 `docs/REMOTE-OPERATIONS.md`。

## 目录

- `config/`：可提交的无密钥配置模板
- `docs/`：实施计划和实验账本
- `scripts/`：Lab 固定入口与验收程序
- `tests/`：Lab 自己的测试
- `secrets/`：本地密钥文件，不提交
- `models/`：Ollama 模型，不提交
- `logs/`、`cache/`、`artifacts/`：运行时及验收产物，不提交
