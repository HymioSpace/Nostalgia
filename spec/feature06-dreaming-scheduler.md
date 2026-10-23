# Scheduled dreaming

状态：Implementing。

Nostalgia owns the periodic dreaming lifecycle. A configured proposal provider may reason about
personal context, but it cannot mutate the database or commit a draft. Nostalgia chooses users,
freezes bounded input, applies the proposal to a draft, validates all references and state changes,
and publishes atomically.

## Scheduling

The default interval is 600 seconds. `NOSTALGIA_DREAMING_INTERVAL_SECONDS` configures it with a
minimum of 60 seconds. One process-wide scheduler skip-overlaps concurrent cycles. For each distinct
configured user it:

1. skips users without pending records or with a live draft;
2. aborts a draft older than `dreaming_draft_timeout_seconds`;
3. selects the oldest pending prefix within record and character limits;
4. creates a draft ending at that deterministic cutoff;
5. calls the configured proposal provider outside a database transaction;
6. applies record decisions, applies artifact changes with `_index` last, and commits with the
   latest draft ETag.

Provider or validation failure aborts the draft and emits `dreaming.failed`. The next interval can
retry. Raw records carry no special scheduler status and remain available through the normal API.

## Provider extension

Set `NOSTALGIA_DREAMING_ENABLED=true` and configure:

```text
dreaming_provider_url
dreaming_provider_token_file
dreaming_batch_max_records
dreaming_batch_max_chars
dreaming_draft_timeout_seconds
```

The provider is an HTTP extension, not an Agent tool. It receives one JSON task:

```json
{
  "user_id": "u1",
  "dream_id": "opaque",
  "etag": "\"dream-opaque-3\"",
  "after_id": "10",
  "through_id": "20",
  "records": [{"id": "11", "root_id": "11", "item_type": "context", "context": {}}],
  "artifacts": [{"id": "_index", "content": "", "links": []}]
}
```

It returns `{artifacts,record_states}`. Each artifact contains its stable `id` plus the protocol
artifact fields. Each record state contains `record_id` plus RecordPatch fields such as
`processing_state`, `status` and `reason`. The response size is bounded to 1 MiB.

Hymio implements this stateless adapter at an internal service endpoint and stores its URL and
service token in private deployment configuration. Hymio does not own the schedule, draft or
publication state. Oath contains no context or dreaming tool.
