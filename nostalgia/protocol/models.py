import hashlib
import json
from datetime import datetime
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator


def bounded_id(value):
    if int(value) >= 2**63:
        raise ValueError("ID exceeds the supported integer range")
    return value


ID = Annotated[str, Field(pattern=r"^[1-9][0-9]*$", max_length=19), AfterValidator(bounded_id)]
CursorID = Annotated[
    str,
    Field(pattern=r"^(0|[1-9][0-9]*)$", max_length=19),
    AfterValidator(bounded_id),
]
Name = Annotated[str, Field(min_length=1, max_length=256)]


def encode(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


class Error(Exception):
    def __init__(self, status: int, code: str, message: str, *, details=None):
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Source(Model):
    kind: Literal["kit", "conversation", "connector", "user", "legacy"] = "legacy"
    parser: Name | None = None
    strategy: Name | None = None
    namespace: Name
    external_id: Name
    version: Name | None = None


class Command(Model):
    command_type: Name
    command_content: str | dict[str, Any]
    target_root_ids: list[ID] = Field(default_factory=list, max_length=200)


class RecordCreate(Model):
    item_type: Literal["context", "command"]
    context_type: Name | None = None
    context: dict[str, Any] | None = None
    command: Command | None = None
    source: Source | None = None
    produced_at: datetime | None = None
    root_id: ID | None = None
    expected_record_id: ID | None = None
    asset_ids: list[Name] = Field(default_factory=list, max_length=100)

    @field_validator("produced_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("produced_at requires a timezone")
        return value

    @model_validator(mode="after")
    def check_payload(self):
        if self.item_type == "context":
            if self.context is None or self.context_type is None or self.command is not None:
                raise ValueError("context requires context_type and context, without command")
        elif self.command is None or self.context is not None or self.context_type is not None:
            raise ValueError("command requires command, without context or context_type")
        if self.item_type == "command" and (self.root_id or self.expected_record_id):
            raise ValueError("command targets belong in command.target_root_ids")
        return self


class RecordPatch(Model):
    processing_state: Literal["applied", "noop", "needs_input"]
    status: Literal["active", "obsolete", "deleted", "pending"] | None = None
    reason: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def check_reason(self):
        if self.processing_state in {"noop", "needs_input"} and not self.reason:
            raise ValueError("noop and needs_input require a reason")
        return self


class Reference(Model):
    root_id: ID
    record_id: ID
    locator: dict[str, Any] | None = None


class Artifact(Model):
    content: str
    artifact_type: Name = "note"
    topics: list[Name] = Field(default_factory=list, max_length=100)
    fields: dict[str, Any] = Field(default_factory=dict)
    references: list[Reference] = Field(default_factory=list, max_length=1000)
    links: list[Name] = Field(default_factory=list, max_length=200)


class RecordQuery(Model):
    dream_id: str | None = None
    root_id: ID | None = None
    source_kind: Literal["kit", "conversation", "connector", "user", "legacy"] | None = None
    namespace: str | None = None
    external_id: str | None = None
    item_type: Literal["context", "command"] | None = None
    context_type: str | None = None
    status: Literal["pending", "active", "obsolete", "deleted"] | None = None
    processing_state: Literal["pending", "applied", "noop", "needs_input"] | None = None
    after_id: CursorID = "0"
    through_id: CursorID | None = None
    produced_after: datetime | None = None
    produced_before: datetime | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    include_deleted: bool = False
    limit: int = Field(default=50, ge=1)
    cursor: str | None = None

    @field_validator("produced_after", "produced_before", "created_after", "created_before")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("time filters require a timezone")
        return value


class Filters(Model):
    ids: list[str] = Field(default_factory=list, max_length=200)
    root_id: ID | None = None
    topic: str | None = None
    source_kind: Literal["kit", "conversation", "connector", "user", "legacy"] | None = None
    namespace: str | None = None
    external_id: str | None = None
    item_type: Literal["context", "command"] | None = None
    context_type: str | None = None
    artifact_type: str | None = None
    status: Literal["pending", "active", "obsolete", "deleted"] | None = None
    processing_state: Literal["pending", "applied", "noop", "needs_input"] | None = None
    produced_after: datetime | None = None
    produced_before: datetime | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None

    @field_validator("produced_after", "produced_before", "created_after", "created_before")
    @classmethod
    def timezone_required(cls, value):
        return RecordQuery.timezone_required(value)


class Search(Model):
    order: Literal["id", "recent"] = "id"
    target: Literal["records", "artifacts", "all"] = "all"
    mode: Literal["filter", "text", "semantic"] = "filter"
    query: str | None = Field(default=None, max_length=4000)
    filters: Filters = Field(default_factory=Filters)
    dream_id: str | None = None
    include_deleted: bool = False
    limit: int = Field(default=50, ge=1)
    cursor: str | None = None


class Rollback(Model):
    view_token: str


class ErrorBody(Model):
    code: str
    message: str
    retryable: bool
    details: list[dict[str, Any]] | None = None


class ErrorResponse(Model):
    error: ErrorBody


class RecordResult(Model):
    id: ID
    root_id: ID
    item_type: Literal["context", "command"]
    context_type: str | None
    context: dict[str, Any] | None
    command: Command | None
    source: Source | None
    asset_ids: list[str]
    produced_at: datetime | None
    created_at: datetime
    status: Literal["pending", "active", "obsolete", "deleted"] | None
    processing_state: Literal["pending", "applied", "noop", "needs_input"]
    reason: str | None


class ArtifactResult(Artifact):
    id: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DreamResult(Model):
    dream_id: str
    state: Literal["draft", "committed", "aborted"]
    after_id: CursorID
    through_id: CursorID
    input_count: int
    handled_count: int
    needs_input_count: int
    snapshot_retained: bool
    created_at: datetime
    completed_at: datetime | None


T = TypeVar("T")


class Page(Model, Generic[T]):
    items: list[T]
    next_cursor: str | None
    visible_through_id: CursorID


class SearchHit(Model):
    updated_at: datetime | None = None
    kind: Literal["record", "artifact"]
    id: str
    root_id: ID | None
    preview: str
    truncated: bool
    status: str | None
    processing_state: str | None
    source: Source | None
    references: list[Reference]
    topics: list[str]
    created_at: datetime


class SearchResult(Page[SearchHit]):
    index_through_id: CursorID
    partial: bool
    search_method: str


class Summary(Model):
    id: Literal["_index"]
    content: str
    links: list[str]


class BufferItem(Model):
    id: ID
    root_id: ID
    item_type: Literal["context", "command"]
    produced_at: datetime | None
    created_at: datetime
    preview: str
    truncated: bool
    pending: Literal[True]


class InjectionResult(Model):
    summary: Summary
    buffer: list[BufferItem]
    view_token: str
    handled_through_id: CursorID
    visible_through_id: CursorID
    pending_count: int
    omitted_count: int
    truncated: bool


class AssetResult(Model):
    id: str
    content_type: str
    size: int


class RollbackResult(Model):
    state: Literal["rolled_back"]
    view_token: str
    after_id: CursorID
    through_id: CursorID
