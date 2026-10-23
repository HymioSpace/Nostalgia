# HTTP API 契约草案

状态：Implemented（MVP）。路径与语义以本文件为准，请求和响应 JSON Schema 由服务的 `/openapi.json` 提供，`/docs` 可交互查看。这里不定义数据库列或模型专用工具。

## 公共规则

- 业务入口为 `/v1`，使用 HTTP + UTF-8 JSON。`user_id` 来自验证后的授权范围，不相信请求正文声称的身份；所有 ID 查询和关联查询都按用户隔离。
- 自增记录 ID 在 JSON 中编码为十进制字符串，其他 ID 为不透明字符串；时间使用带时区的 RFC 3339。来源生产时间与服务接收时间分开。
- 写请求携带 `Idempotency-Key`。同用户、同操作的重复键和相同语义请求返回原结果；同键不同内容返回 `409`。已接纳原始记录的重试身份随记录保留。
- 草稿条目修改使用服务返回的 `ETag` 与 `If-Match`；创建条目使用 `If-None-Match: *`。幂等重试先返回已完成结果，不因旧 ETag 再报错。
- 响应为资源对象，列表统一返回 `items`、`next_cursor`。分页游标固定过滤条件及读取范围；已切换且无法继续原范围时返回冲突，不能混合两个发布结果。
- `201` 表示已创建并可靠保存；其他成功读取或状态操作使用 `200`，草稿删除成功使用 `204`。原始写入成功不代表意图已执行。
- 错误返回 `error.code / message / retryable`，按需带有限的冲突对象 ID。`400` 无效输入，`401/403` 身份或权限失败，`404` 不存在或不可见，`409` 生命周期或幂等冲突，`412` 条目或来源版本前置条件不满足，`422` 引用或提交检查不通过，`413` 超过大小限制，`415` 内容类型不支持，`503` 暂不可用。
- 新增可选字段允许兼容扩展；客户端忽略未知响应字段，未知 command type 保留原意图并明确报告未支持，不能默认为删除或修改。破坏性协议变化使用新 API 主版本。

## 原始记录

### `POST /v1/records`

追加一个 context 或 command。接收来源身份、生产时间、JSON 内容、幂等身份，以及可选根 ID、来源版本与预期前序记录。首次写入由服务生成 ID 并建立根 ID；变更引用已有根 ID。command 包含类型和内容，内容可为自然语言字符串并带可选目标引用。

返回原始行 ID、根 ID、服务写入时间和待整理状态，只有持久化后才能返回。原始行不提供普通覆盖或立即删除接口；后续操作继续追加。

同源重试不重复建根。已有 context 根的非重复追加必须提供最新的 `expected_record_id`，并保持来源身份不变；绑定已有根、预期前序或来源版本不一致时返回冲突，不静默拼接到错误历史。command 正文作为待解释的用户意图，普通 context 正文中的命令句不自动变成操作请求。内置 `restore` 类型用于明确恢复已删除根，其余类型由整理 Agent 解释。

### `GET /v1/records` 与 `GET /v1/records/{id}`

读取原始列表或全文。列表支持根 ID、来源、item/context 类型、状态、生产/写入时间、`after_id` 与 `through_id`，按 ID 稳定分页。按根 ID 获取完整历史不依赖搜索索引。

传 `dream_id` 时限定在本次固定截止位置内，并返回草稿中暂存的状态；不传时返回最新已发布状态及待整理记录。全文结果包含内容或可读取的资源引用。

### `PATCH /v1/records/{id}?dream_id=...`

只允许在未结束草稿中记录处理结论、原因和拟调整状态，不允许修改原始正文或改根 ID。Agent 可以处理本批输入以及 H 以内需要调整的历史记录。普通调用方通过追加 command 请求这些变化。

同根历史允许旧行过时、新行生效；将根标记删除时，该根在本批范围内的内容都退出默认查询，H 之后的输入留待下批审阅。待澄清保留可查询状态，即使本批随后提交，也不能显示为已执行。

## 制品与检索

### `GET /v1/artifacts/{id}`

读取生效制品及来源引用。指定 `dream_id` 时读取该草稿中修改后的结果。无草稿改动的条目继承开始整理时的生效内容。公开读接口不接受内部版本号。

### `PUT /v1/artifacts/{id}?dream_id=...`

在草稿中创建或整体替换一个制品。ID 在后续整理中保持稳定；内容包括正文、可选类型/topic/结构化信息，以及原始根 ID 与实际依据行 ID 的引用。允许可选的制品关联。

`id=_index` 保留给摘要索引，使用相同接口维护。它保存有界的摘要文本和可展开制品引用，来源经被引用制品追溯。本批必须显式保存一次；其后若再修改普通制品或原始状态，需重新确认并保存索引。普通制品至少有一份可定位依据；删除依据时要同时修订制品引用和内容。

### `DELETE /v1/artifacts/{id}?dream_id=...`

从草稿结果中移除该制品；提交前不影响普通读取。摘要索引必须存在，空库使用空索引。提交前必须修复摘要或其他制品中指向已移除制品的引用。

### `POST /v1/search`

一个检索入口，目标为原始记录、制品或两者，指定可选文本、过滤条件、分页与可选 `dream_id`。支持 `filter`、`text`，有能力的实现可支持 `semantic`；不支持的方式返回明确错误，不能静默换方式。

过滤包含 ID、来源根 ID、topic、类型、状态和时间。制品的来源根 ID 过滤表示精确的反向溯源，必须完整分页；纯过滤查询读取权威记录。文本/语义搜索返回排序后的有限命中，不承诺检索完整性。

命中返回 ID、预览、来源引用、处理/生效状态和截断标记。语义分数只表示排序依据。响应报告可见内容截至范围、索引覆盖范围和是否部分返回；使用者可按 ID/根 ID 精确读取近期内容。

草稿搜索必须能找到已经创建或修改的制品，即使持久索引尚未更新；实现可直接检索有界草稿。已有精确过滤、读取和来源引用足以支持关联展开，MVP 不新增专用 `recall` 或图遍历 API。

## 整理生命周期

### `POST /v1/dreams`

开始一次整理，返回 `dream_id`、已处理起点、固定截止 ID、状态和草稿修改标识。复制逻辑视图即可，不要求复制全部物理数据。每用户只允许一个未结束草稿，另一次请求返回 `409`；相同幂等请求返回原整理。

### `GET /v1/dreams/{id}`

查询输入范围、状态、处理进度、待澄清数量和提交检查结果，用于恢复和重试。返回当前整个草稿的 `ETag`；原始状态、制品或摘要的任何草稿写入都会更新它，该标识与单个条目的 ETag 分开。处理进度以已保存条目为准，不以模型声称完成为准；具体待澄清记录按状态分页读取。终态为已提交或已放弃，结束后不能继续修改。

### `POST /v1/dreams/{id}/commit`

请求提交，通过 `If-Match` 携带读取到的整个草稿 ETag，防止遗漏最后一次并发工具写入。提交检查通过后原子发布制品、摘要、原始状态和处理游标。提交检查失败保留草稿，返回待修复项，Agent 可以继续读取和修改。

校验使用本次固定范围及拟发布状态，不因 H 之后新增的内容或 command 自动重算本批。提交与工具写入短时串行；提交完成后迟到的写入被拒绝。同一整理重复提交返回同一终态。

### `POST /v1/dreams/{id}/abort`

放弃草稿，生效内容和处理游标不变。重复放弃幂等；已提交整理不能再放弃。

### `POST /v1/dreams/rollback`

回退最近一次发布，不接受内部版本号。请求带最近读取的生效视图标识和幂等键，避免并发发布或重试造成连续回退；回退已完成时重试返回同一结果。没有保留历史或存在草稿时返回 `409`。

回退恢复前一生效结果及其原始处理/生效状态和游标，包括 command 的审阅结论；当前原始日志不回退，恢复游标后的记录重新标记待整理。响应明确返回被重新纳入整理的范围。内部版本历史不影响身份授权。

## 注入读取

### `GET /v1/injection`

一次返回当前摘要索引、近期 buffer、未展示待整理记录数和截断情况。还返回不透明的生效视图标识，供回退前置条件使用；普通调用方不通过它选择版本。

buffer 项至少包含原始行 ID、根 ID、item 类型、生产/写入时间、有限预览和待整理标记。全文通过原始读取获取。MVP 采用服务端公布的上限，不让调用方绕过总长度限制。

摘要与 buffer 读取必须一致。新发布使用新摘要和新游标；旧视图仍使用旧摘要和旧游标，不能先清 buffer 再发布摘要。

待澄清是已经审阅但尚未解决的请求，可通过原始记录状态过滤查询；它不因超过游标或 buffer 淘汰而丢失。注入接口不把所有未解决请求无限追加到 buffer。

## 资源与能力

### `POST /v1/assets` 与 `GET /v1/assets/{id}`

上传和读取原始资源。MVP 上传使用原始二进制请求体，`Content-Type` 表示资源类型，返回持久资源 ID 和类型/大小。原始记录通过 `asset_ids` 关联资源，其他结构化信息仍放在 context JSON。读取仍校验用户权限，不返回内部文件路径；上传大小受限制。原始记录不能引用其他用户或未成功上传的资源。

### `GET /v1/capabilities` 与 `GET /healthz`

能力接口返回协议版本、支持的检索方式、JSON/资源大小、分页、摘要/buffer 上限和历史保留配置。健康接口用于 Docker 服务探测，不返回私人内容。

## MVP 调用顺序

Hymio 追加记录后可立即读取注入内容。开始整理后，通过原始列表完整读取固定输入，使用同一查询接口读取关联历史，修改草稿制品和处理状态，更新 `_index`，最后提交。普通 Agent 只需注入、搜索与按 ID 读取。

接口实现语言、部署框架、数据库方言、索引引擎与 Agent 工具名不属于本契约。当前文本查询使用不区分大小写的子串匹配，按稳定 ID 顺序分页；能力信息明确声明该方法，不把它当作语义或相关度检索。后续增加检索实现不改变上述状态与发布语义。

## Layered assembly and durable events

`GET /v1/assembly` separates stable `{content,cache_key}` (only `_core` content) from
`dynamic` (the existing injection response), plus `view_token` and `events_url`.
Do not put dynamic navigation/buffer into a cache-sensitive system prefix.
`_core` is an optional bounded artifact with mandatory source references; it uses the same
ETag/dream atomic publication protocol. Ordinary dreaming preserves it.

`GET /v1/events?after=0&limit=50` returns user-scoped ordered items `{id,type,data,created_at}`,
`next_after`, `has_more` and `delivery=at_least_once`. Record appends, context publication and
rollback emit transactionally with the mutation. Exact idempotent retries emit no new event.
Consumers persist their checkpoint only after durable acceptance, and deduplicate by event ID.
Events contain invalidation IDs; fetch current authorized details instead of trusting stale previews.

Source adds kind (`kit|conversation|connector|user|legacy`), parser and strategy (version hints).
Old sources default to legacy. `source_kind` is available on record lists and search filters.
Text retrieval remains an authoritative substring scan, not semantic search or a recall guarantee.
