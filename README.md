# Nostalgia

Nostalgia 是 Hymio 的个人 Context 服务。它保存原始内容与变更意图，供 dreaming Agent 整理成可溯源的制品，并向主 Agent 提供摘要索引、近期 buffer 和基础检索。

MVP 已提供独立 HTTP 服务、持久化存储和 Docker 部署。Hymio 负责采集、解析、Agent tool/skill、调用 Oath 与注入；Nostalgia 维护一套 API，不内置模型或 Agent runtime。

- [MVP 架构与核心机制](spec/overview.md)
- [HTTP API 契约草案](spec/api.md)
- [场景与验收](spec/scenarios.md)
- [调用示例](spec/usage.md)

## 启动

需要 Python 3.11+ 与 uv，或者 Docker Compose。先配置 bearer token 到用户的映射，替换下方示例 token；服务不会提供默认凭据。

```sh
export NOSTALGIA_TOKENS_JSON='{"replace-with-your-own-random-token":"local"}'
uv sync --frozen
uv run nostalgia
```

默认监听 `0.0.0.0:8000`，本机数据库为 `data/nostalgia.sqlite3`。`NOSTALGIA_HOST`、`NOSTALGIA_PORT`、`NOSTALGIA_DATABASE` 可覆盖配置。业务 API 使用 `Authorization: Bearer <token>`，写操作另带 `Idempotency-Key`。

Docker 启动复用同一份 token 配置：

```sh
docker compose up --build -d
```

Compose 默认只将端口发布到本机 `127.0.0.1:8000`，通过 `NOSTALGIA_PUBLISHED_PORT` 更换。数据保存在命名卷 `nostalgia-data`。基础镜像默认 `python:3.11-slim`，可通过 `NOSTALGIA_PYTHON_IMAGE` 指定镜像源，例如 `public.ecr.aws/docker/library/python:3.11-slim`。

服务提供 `/healthz`、交互式协议文档 `/docs` 和 `/openapi.json`；能力、限制和支持的检索方式在经过认证的 `/v1/capabilities` 中查询。

## Dreaming scheduler

Nostalgia owns the dreaming cycle. When enabled, it wakes every ten minutes by default, finds
users with pending records, creates one bounded draft, asks the configured proposal provider for
structured edits, validates them through the same draft/commit state machine, and publishes
atomically. The provider never writes storage directly.

```sh
NOSTALGIA_DREAMING_ENABLED=true
NOSTALGIA_DREAMING_INTERVAL_SECONDS=600
NOSTALGIA_DREAMING_PROVIDER_URL=http://backend:8080/internal/dreaming/propose
NOSTALGIA_DREAMING_PROVIDER_TOKEN_FILE=/run/secrets/hymio-service-token
```

`dreaming_batch_max_records`, `dreaming_batch_max_chars` and
`dreaming_draft_timeout_seconds` bound unattended work. A provider receives
`{user_id,dream_id,etag,after_id,through_id,records,artifacts}` and returns
`{artifacts,record_states}`. Nostalgia applies record decisions first, the `_index` artifact last,
then commits with fresh validation. Failed proposals abort their draft so the next cycle retries.
This is internal lifecycle code, not an Agent tool.

## 实现范围

API、Core、Storage 分层。MVP 使用 SQLite 事务存储三类核心记录，以及幂等回执和资源等
辅助数据；二进制资源也保存在同一持久数据库中。备份时使用 SQLite 在线备份能力或停止
服务后备份整个数据目录，不能只在运行中复制主数据库而忽略 WAL。

代码按 `api/`（HTTP 适配）、`core/`（整理与查询）、`protocol/`（协议模型）、`storage/`
（持久化）、`dreaming.py`（周期调度）和 `bootstrap/`（配置与启动）分目录。Hymio 的
`extensions/tools/context_memory.py` 只暴露 `remember` 与 `recall`，通过 Hymio Backend
使用本服务；Oath 不直接依赖本服务的 Python 包。

制品采用独立卡片，可带 topic、结构化字段、关联和多份原始来源。草稿按完整快照保存，发布与回滚同时处理制品、摘要、原始状态和游标。保留当前及最近几个可回退结果，默认最多 5 份；过期制品详情清理后仍保留原始记录、任务终态与幂等回执。

文本检索暂为不区分大小写的子串匹配，按稳定 ID 顺序分页，不提供相关度排序或语义搜索。精确过滤、原文定位、根 ID 历史与反向溯源均读取权威记录；接口与持久化索引实现分离，后续可增加索引。

整理的调度、输入范围、草稿、校验和提交由 Nostalgia 负责；配置的 dreamer 只返回结构化
提案。接收 command 不会直接执行其中的自然语言。

## 测试

```sh
uv run ruff check nostalgia tests scripts
uv run pytest -q
uv run python scripts/smoke.py
docker build -t nostalgia:mvp .
uv run python scripts/smoke.py --docker
```

HTTP smoke 测试会创建独立临时数据库或容器/卷，走完整整理、提交、回滚和延后删除流程，并强制结束服务两次检查持久化恢复；结束后清理它自己创建的测试资源。它不会连接已有业务服务。

依赖由 `uv.lock` 固定，Docker 使用从锁文件导出的 `requirements.txt`。更新依赖后重新导出：

```sh
uv export --no-dev --no-hashes --no-emit-project --format requirements-txt --output-file requirements.txt
```

Layered stable context, source kinds, the scheduled dreaming lifecycle and a durable invalidation
feed are documented in [Context foundation](spec/feature05-context-foundation.md).
