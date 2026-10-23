# Context foundation

Status: Implemented (MVP). Extends PR #1 without replacing raw records/dream atomic publication.

Sources have kind (kit, conversation, connector, user, legacy), namespace, external_id,
version and optional parser/strategy version. These are provenance and routing hints, never
permission claims. Legacy sources remain readable. Kit projections represent committed business
facts; conversations represent statements; connectors retain external revisions; user saves express
explicit interest. Hymio owns acquisition/parsing; Nostalgia owns attributable revisions and retrieval.

Injection is partitioned: `_core` is a bounded, referenced, explicitly edited artifact for a stable
system prefix; `_index` remains the evolving navigation summary. Ordinary dreaming preserves `_core`.
A dedicated assembly response fingerprints only `_core` content, so appends and ordinary commits do
not invalidate that prefix. Dynamic events are a transactionally written, user-scoped ordered feed.
Consumers read after a durable checkpoint and acknowledge only after their callback succeeds;
delivery is at least once and callback consumers deduplicate by event ID. No queue integration is
claimed in this MVP. Rollbacks also publish invalidations. Events contain IDs, not private raw text.

Search keeps exact reads, filters, provenance, view/cursor and index watermark semantics. Text scan
is an honest reference implementation, not semantic retrieval. Source-kind filters are additive.
Future ranked/hybrid providers must preserve these fields and report actual method and coverage.
Precision/recall require a labeled corpus before any quality claim.

Compatibility: old injection/search endpoints and source namespace identity are retained. New
source fields default to legacy; namespace+external_id remains the identity key. New endpoints are
additive. SQLite creates the event table idempotently; existing record JSON needs no migration.
