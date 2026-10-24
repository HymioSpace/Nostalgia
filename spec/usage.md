# MVP 调用示例

服务运行后可在 `/docs` 查看请求和响应字段。以下使用 JSON 展示业务对象；所有请求携带 bearer token，所有写操作带一个稳定的 `Idempotency-Key`，重试时复用原键和原请求。

## 导入与注入

`POST /v1/records`：

```json
{
  "item_type": "context",
  "context_type": "message",
  "context": {"text": "Hymio 给个人使用，不打算商业化。"},
  "source": {"namespace": "chat/account-a", "external_id": "message-1", "version": "1"},
  "produced_at": "2026-10-20T09:00:00+08:00"
}
```

响应返回字符串形式的 `id`、`root_id` 和待整理状态。通过 `GET /v1/injection` 读取摘要和近期 buffer，使用 `GET /v1/records/{id}` 阅读未截断内容。`user_id` 不从正文接收。

修改同一来源内容时追加新的 context，携带已有 `root_id`、最新行的 `expected_record_id` 和新的来源版本。请求删除时追加 command：

```json
{
  "item_type": "command",
  "command": {
    "command_type": "delete",
    "command_content": "删除之前关于 Hymio 商业化的错误计划",
    "target_root_ids": ["1"]
  }
}
```

暂时不知道目标时可以省略目标列表，由 dreaming 检索定位。收到请求不代表删除已执行。

## 一次整理

`POST /v1/dreams` 开始整理，得到 `dream_id`、`after_id`、`through_id` 和草稿 ETag。使用 `GET /v1/records?dream_id=...&after_id=...&through_id=...` 分页读取本批全部记录；不要用近期 buffer 代替完整输入。

使用 `POST /v1/search` 查历史和已整理内容：

```json
{
  "target": "artifacts",
  "mode": "filter",
  "dream_id": "本次整理ID",
  "filters": {"root_id": "1"},
  "limit": 50
}
```

这是按来源根 ID 精确查关联制品，跟随 `next_cursor` 能读完。查找模糊线索时改用 `mode=text` 和 `query`，可加 topic、时间等过滤条件。

在草稿内 `PUT /v1/artifacts/hymio-purpose?dream_id=...` 创建制品，带 `If-None-Match: *`：

```json
{
  "content": "Hymio 面向个人使用，不计划商业化。",
  "topics": ["Hymio"],
  "references": [{"root_id": "1", "record_id": "1"}]
}
```

修改已有制品先 GET 同一草稿下的条目，再用返回的 ETag 作为 `If-Match`。删除制品使用同样的前置条件。服务返回的 `id`、时间等只读字段不用放回 PUT 正文。

逐条 `PATCH /v1/records/{id}?dream_id=...` 保存审阅结论，使用该草稿 GET 原始记录得到的 ETag：

```json
{"processing_state": "applied", "status": "active"}
```

command 没有内容状态，使用 `{"processing_state":"applied"}`。不必执行的请求可用 `noop`，不确定的请求可用 `needs_input`，这两种结论都要求 `reason`。删除根使用其任意原始 context 行并设置 `status=deleted`，再处理引用该根的制品；更换同根版本时把旧行标为 `obsolete`，新行标为 `active`。

最后保存 `PUT /v1/artifacts/_index?dream_id=...`：

```json
{"content": "Hymio：个人 Agent 项目，定位与部署思路见 purpose。", "links": ["hymio-purpose"]}
```

若 `_index` 已存在，先读取后携带 `If-Match`；首次创建用 `If-None-Match: *`。摘要之后若再修改制品或原始状态，需要重新确认并保存摘要。

再次 `GET /v1/dreams/{id}` 获取整个草稿的最新 ETag，再 `POST /v1/dreams/{id}/commit`，用 `If-Match` 携带它。提交失败会报告遗漏的输入、悬空关联或未处理的删除引用，修改后可重新提交。成功后新的制品和摘要一起生效，buffer 仅移除本批范围。

## 继续、放弃与回退

进程重启后，通过已保存的 `dream_id` 查询并继续；Hymio 应持久保存开始请求的幂等键或返回 ID。`POST /v1/dreams/{id}/abort` 放弃本批，不改变已发布结果。

从 `/v1/injection` 获取最新 `view_token`，通过 `POST /v1/dreams/rollback` 提交 `{"view_token":"..."}` 回退一次。重试继续使用原幂等键与原 token，避免连续回退。原始日志保留，游标之后的记录重新待整理。
