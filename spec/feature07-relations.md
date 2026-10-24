# Versioned card relations

Status: Implementing.

Relations connect any record/artifact pair, independent of category or source. Store explicit
relations in version details, alongside artifacts, so draft, commit, rollback and retention are
atomic. Existing references and links remain authoritative legacy inputs and are projected as
supports (record → artifact) and navigates (artifact → artifact), never inferred causal edges.
Explicit relations use stable deterministic IDs, endpoint revisions, evidence, rationale,
origin and active/disputed/retired states. Symmetric edges normalize endpoints. Reads are scoped
to one user's current snapshot; a changed snapshot returns 409 instead of mixing pages.

Graph queries are bounded, include both edge directions, allow any node as seed, return
truncation and hidden-neighbor counts. Record text stays immutable. Model proposals may add,
revise or retire relationships in the same dream as understanding and overview updates.
Draft validation rejects missing/deleted endpoints, unsupported evidence, duplicate semantic
edges, self edges and supersedes cycles. Related edges do not imply evidential support.

Compatibility: old clients and providers continue working with references/links and optional
relations omitted. The graph adds read and draft-write APIs; it does not silently change literal
search. Structured graph capability is advertised. No second graph store is introduced.
