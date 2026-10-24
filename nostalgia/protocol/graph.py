from typing import Literal

from pydantic import Field, model_validator

from .models import Model, Name, Reference


class NodeRef(Model):
    kind: Literal["record", "artifact"]
    id: Name

    @property
    def key(self):
        return f"{self.kind}:{self.id}"


class Relation(Model):
    source: NodeRef
    target: NodeRef
    type: Literal["supports", "supplements", "contradicts", "supersedes", "related", "navigates"]
    status: Literal["active", "disputed", "retired"] = "active"
    origin: Literal["model", "user", "import"] = "model"
    rationale: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[Reference] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def normalize(self):
        if self.source.key == self.target.key:
            raise ValueError("Self relations are not supported")
        if self.type in {"related", "contradicts"} and self.source.key > self.target.key:
            self.source, self.target = self.target, self.source
        if self.status != "retired" and self.origin == "model" and not self.evidence_refs:
            raise ValueError("Model relations require original evidence")
        return self


class GraphQuery(Model):
    seed: NodeRef | None = None
    query: str = Field(default="", max_length=500)
    depth: int = Field(default=1, ge=1, le=2)
    limit: int = Field(default=24, ge=1, le=60)
    offset: int = Field(default=0, ge=0, le=100000)
    snapshot: str | None = None
    changes_only: bool = False


class RetrieveQuery(Model):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=40)
