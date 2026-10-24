import json
import re

from ..bootstrap.config import Settings
from ..protocol import Error, encode, fingerprint
from ..storage import Store, identifier, now


class Core:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings

    @staticmethod
    def active(repo, user):
        return repo.one("versions", user_id=user, state="active")

    @staticmethod
    def dream(repo, user, dream_id, *, editable=False):
        row = repo.one("versions", id=dream_id, user_id=user)
        if not row or row["base_id"] is None:
            raise Error(404, "not_found", "Dream not found")
        if editable and row["state"] != "draft":
            raise Error(409, "dream_closed", "Dream is no longer editable")
        if editable and not row["retained"]:
            raise Error(409, "snapshot_expired", "This dream's snapshot has expired")
        return row

    def view(self, repo, user, dream_id=None):
        if dream_id:
            row = self.dream(repo, user, dream_id, editable=True)
            return row, row["cutoff"], self.dream_etag(row)
        row = self.active(repo, user)
        return row, repo.head(user), row["view_token"] if row else "empty"

    @staticmethod
    def dream_etag(row):
        return f'"dream-{row["id"]}-{row["revision"]}"'

    @staticmethod
    def check_match(current, match, none_match=None):
        if current is None:
            if none_match != "*":
                raise Error(412, "precondition_failed", "Create requires If-None-Match: *")
        elif match != current:
            raise Error(412, "precondition_failed", "Read the current resource ETag first")

    @staticmethod
    def detail(repo, version, kind, item_id):
        return repo.one("details", version_id=version["id"], kind=kind, item_id=str(item_id))

    @staticmethod
    def save_detail(repo, version, kind, item_id, value):
        where = {"version_id": version["id"], "kind": kind, "item_id": str(item_id)}
        previous = repo.one("details", **where)
        if previous:
            repo.update(
                "details",
                where,
                data=encode(value),
                revision=version["revision"] + 1,
                updated_at=now(),
            )
        else:
            timestamp = now()
            repo.insert(
                "details",
                **where,
                data=encode(value),
                revision=version["revision"] + 1,
                created_at=timestamp,
                updated_at=timestamp,
            )

    @staticmethod
    def bump(repo, version, *, index_ready=False):
        repo.update(
            "versions",
            {"id": version["id"]},
            revision=version["revision"] + 1,
            index_ready=int(index_ready),
        )

    def record_view(self, repo, row, version=None):
        data = json.loads(row["data"])
        state = {key: row[key] for key in ("status", "processing_state", "reason")}
        detail = self.detail(repo, version, "record_state", row["id"]) if version else None
        if detail:
            state = json.loads(detail["data"])
        return {
            "id": str(row["id"]),
            "root_id": str(row["root_id"]),
            "created_at": row["created_at"],
            **data,
            **state,
        }

    @staticmethod
    def record_etag(row, version, detail=None):
        revision = detail["revision"] if detail else 0
        return f'"record-{version["id"] if version else "empty"}-{row["id"]}-{revision}"'

    @staticmethod
    def artifact_etag(version, detail):
        return f'"artifact-{version["id"]}-{detail["item_id"]}-{detail["revision"]}"'

    def get_record(self, repo, user, record_id, dream_id=None):
        version, cutoff, _ = self.view(repo, user, dream_id)
        row = repo.one("records", id=int(record_id), user_id=user)
        if not row or row["id"] > cutoff:
            raise Error(404, "not_found", "Record not found")
        detail = self.detail(repo, version, "record_state", record_id) if version else None
        return self.record_view(repo, row, version), self.record_etag(row, version, detail)

    def append(self, repo, user, payload):
        data = payload.model_dump(mode="json", exclude={"root_id", "expected_record_id"})
        source = data.get("source") or {}
        root_id = int(payload.root_id) if payload.root_id else None
        original = repo.one("records", id=root_id, user_id=user) if root_id else None
        if root_id and (
            not original or original["item_type"] != "context" or original["root_id"] != root_id
        ):
            raise Error(404, "not_found", "Context root not found")
        if original:
            original_source = json.loads(original["data"]).get("source")
            if bool(original_source) != bool(source) or (
                original_source
                and any(
                    source.get(k) != original_source.get(k) for k in ("namespace", "external_id")
                )
            ):
                raise Error(412, "source_conflict", "Root belongs to another source")
        existing = []
        if source:
            existing = repo.find(
                "records",
                user_id=user,
                namespace=source["namespace"],
                external_id=source["external_id"],
            )
            if existing:
                oldest = min(existing, key=lambda r: r["id"])
                if oldest["item_type"] != payload.item_type:
                    raise Error(412, "source_conflict", "Source item type cannot change")
                if root_id is not None and root_id != oldest["root_id"]:
                    raise Error(412, "source_conflict", "Source already belongs to another root")
                root_id = oldest["root_id"]
        signature = fingerprint(data)
        for row in existing:
            same_version = source.get("version") is not None and (
                row["source_version"] == source["version"]
            )
            same_content = row["signature"] == signature
            if same_version and not same_content:
                raise Error(412, "source_conflict", "Source version has different content")
            if same_version or (same_content and row["source_version"] is None):
                return self.record_view(repo, row, self.active(repo, user))
        if root_id:
            history = repo.records(user, root_id=root_id)
            latest = max(history, key=lambda r: r["id"])
            if payload.expected_record_id != str(latest["id"]):
                raise Error(412, "source_conflict", "Update requires the latest expected_record_id")
        elif payload.expected_record_id:
            raise Error(412, "source_conflict", "New root cannot have a predecessor")
        for asset_id in payload.asset_ids:
            if not repo.one("assets", id=asset_id, user_id=user):
                raise Error(422, "invalid_asset", "Asset is missing or belongs to another user")
        if payload.command:
            for target in payload.command.target_root_ids:
                target_row = repo.one("records", id=int(target), user_id=user)
                if not target_row or target_row["root_id"] != int(target):
                    raise Error(422, "invalid_target", "Command target root not found")
        record_id = repo.insert(
            "records",
            user_id=user,
            root_id=root_id,
            item_type=payload.item_type,
            data=encode(data),
            signature=signature,
            namespace=source.get("namespace"),
            external_id=source.get("external_id"),
            source_version=source.get("version"),
            produced_at=data.get("produced_at"),
            created_at=now(),
            status="pending" if payload.item_type == "context" else None,
        )
        if root_id is None:
            repo.update("records", {"id": record_id}, root_id=record_id)
        self.emit(repo, user, "record.appended", {"record_id": str(record_id)})
        return self.record_view(repo, repo.one("records", id=record_id), self.active(repo, user))

    def patch_record(self, repo, user, record_id, dream_id, patch, match):
        version = self.dream(repo, user, dream_id, editable=True)
        current, etag = self.get_record(repo, user, record_id, dream_id)
        self.check_match(etag, match)
        if current["item_type"] == "command" and patch.status is not None:
            raise Error(400, "invalid_state", "Command has a processing state, not content status")
        if current["item_type"] == "context":
            if patch.status is None:
                raise Error(400, "invalid_state", "Context processing requires a content status")
            if patch.processing_state == "applied" and patch.status == "pending":
                raise Error(400, "invalid_state", "Applied context cannot remain pending")
        value = patch.model_dump()
        if current["item_type"] == "context" and patch.status == "deleted":
            for row in repo.records(user, version["cutoff"], root_id=current["root_id"]):
                self.save_detail(repo, version, "record_state", row["id"], value)
        else:
            self.save_detail(repo, version, "record_state", record_id, value)
        self.bump(repo, version)
        return self.get_record(repo, user, record_id, dream_id)

    def artifact(self, repo, user, item_id, dream_id=None):
        version, _, _ = self.view(repo, user, dream_id)
        detail = self.detail(repo, version, "artifact", item_id) if version else None
        if not detail:
            if item_id == "_index" and not dream_id:
                return self.empty_index(), '"empty-index"'
            raise Error(404, "not_found", "Artifact not found")
        return {
            "id": item_id,
            **json.loads(detail["data"]),
            "created_at": detail["created_at"],
            "updated_at": detail["updated_at"],
        }, (self.artifact_etag(version, detail))

    @staticmethod
    def empty_index():
        return {
            "id": "_index",
            "content": "",
            "artifact_type": "index",
            "topics": [],
            "fields": {},
            "references": [],
            "links": [],
        }

    def validate_references(self, repo, user, version, artifact):
        if not artifact.references:
            raise Error(422, "missing_references", "Artifacts require original source references")
        for ref in artifact.references:
            row = repo.one("records", id=int(ref.record_id), user_id=user)
            if not row or row["id"] > version["cutoff"] or str(row["root_id"]) != ref.root_id:
                raise Error(422, "invalid_reference", "Reference is outside this dream's input")

    def put_artifact(self, repo, user, item_id, dream_id, artifact, match, none_match):
        if item_id not in {"_index", "_core"} and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", item_id
        ):
            raise Error(400, "invalid_id", "Artifact ID must be a short URL-safe identifier")
        version = self.dream(repo, user, dream_id, editable=True)
        detail = self.detail(repo, version, "artifact", item_id)
        etag = self.artifact_etag(version, detail) if detail else None
        self.check_match(etag, match, none_match)
        value = artifact.model_dump(mode="json")
        if item_id == "_index":
            value["artifact_type"] = "index"
            if len(encode({"id": item_id, **value})) > self.settings.index_max_chars:
                raise Error(413, "index_too_large", "Summary index exceeds configured limit")
        else:
            self.validate_references(repo, user, version, artifact)
            if item_id == "_core" and len(artifact.content) > self.settings.index_max_chars:
                raise Error(413, "core_too_large", "Stable core exceeds configured limit")
        self.save_detail(repo, version, "artifact", item_id, value)
        self.bump(repo, version, index_ready=item_id == "_index")
        data, tag = self.artifact(repo, user, item_id, dream_id)
        return (200 if detail else 201), data, tag

    def delete_artifact(self, repo, user, item_id, dream_id, match):
        if item_id == "_index":
            raise Error(409, "index_required", "Replace the index; do not delete it")
        version = self.dream(repo, user, dream_id, editable=True)
        _, etag = self.artifact(repo, user, item_id, dream_id)
        self.check_match(etag, match)
        repo.delete("details", version_id=version["id"], kind="artifact", item_id=item_id)
        self.bump(repo, version)

    def start_dream(self, repo, user, cutoff=None):
        if repo.one("versions", user_id=user, state="draft"):
            raise Error(409, "dream_exists", "Finish or abort the existing dream first")
        base = self.active(repo, user)
        cutoff = repo.head(user) if cutoff is None else int(cutoff)
        if not base["cursor"] <= cutoff <= repo.head(user):
            raise Error(400, "invalid_cutoff", "Dream cutoff must follow the active cursor")
        dream_id = identifier()
        repo.insert(
            "versions",
            id=dream_id,
            user_id=user,
            state="draft",
            base_id=base["id"],
            cursor=base["cursor"],
            cutoff=cutoff,
            view_token=identifier(),
            created_at=now(),
            input_after=base["cursor"],
            revision=base["revision"],
        )
        for detail in repo.find("details", version_id=base["id"]):
            repo.insert("details", **{**detail, "version_id": dream_id})
        return self.dream_info(repo, user, dream_id)

    def dream_info(self, repo, user, dream_id):
        row = repo.one("versions", id=dream_id, user_id=user)
        if not row or row["base_id"] is None:
            raise Error(404, "not_found", "Dream not found")
        if not row["retained"] and row["report"]:
            data = json.loads(row["report"])
            data["snapshot_retained"] = False
            return data, self.dream_etag(row)
        states = {
            d["item_id"]: json.loads(d["data"])
            for d in repo.find("details", version_id=dream_id, kind="record_state")
        }
        inputs = [r for r in repo.records(user, row["cutoff"]) if r["id"] > row["input_after"]]
        handled = sum(
            states.get(str(r["id"]), {}).get("processing_state", "pending") != "pending"
            for r in inputs
        )
        data = {
            "dream_id": dream_id,
            "state": {
                "active": "committed",
                "inactive": "committed",
                "rolled_back": "committed",
            }.get(row["state"], row["state"]),
            "after_id": str(row["input_after"]),
            "through_id": str(row["cutoff"]),
            "input_count": len(inputs),
            "handled_count": handled,
            "needs_input_count": sum(
                s["processing_state"] == "needs_input" for s in states.values()
            ),
            "snapshot_retained": bool(row["retained"]),
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }
        return data, self.dream_etag(row)

    def commit(self, repo, user, dream_id, match):
        version = self.dream(repo, user, dream_id)
        if version["state"] in {"active", "inactive", "rolled_back"}:
            return self.dream_info(repo, user, dream_id)
        if version["state"] != "draft":
            raise Error(409, "dream_closed", "Cannot commit an aborted dream")
        self.check_match(self.dream_etag(version), match)
        details = repo.find("details", version_id=dream_id)
        states = {
            d["item_id"]: json.loads(d["data"]) for d in details if d["kind"] == "record_state"
        }
        artifacts = {
            d["item_id"]: json.loads(d["data"]) for d in details if d["kind"] == "artifact"
        }
        inputs = repo.records(user, version["cutoff"])
        errors = []
        for row in inputs:
            state = states.get(str(row["id"]))
            if row["id"] > version["cursor"] and (
                not state or state["processing_state"] == "pending"
            ):
                errors.append({"record_id": str(row["id"]), "code": "unhandled_input"})
        if not version["index_ready"] or "_index" not in artifacts:
            errors.append({"code": "index_not_confirmed"})
        record_by_id = {str(r["id"]): r for r in inputs}
        deleted_roots = {
            str(r["root_id"])
            for r in inputs
            if states.get(str(r["id"]), {}).get("status") == "deleted"
        }
        active_roots = {}
        for row in inputs:
            if states.get(str(row["id"]), {}).get("status") == "active":
                root = str(row["root_id"])
                if root in active_roots:
                    errors.append({"root_id": root, "code": "multiple_active_records"})
                active_roots[root] = str(row["id"])
        for root in deleted_roots & active_roots.keys():
            errors.append({"root_id": root, "code": "root_state_conflict"})
        for item_id, artifact in artifacts.items():
            for target in artifact["links"]:
                if target not in artifacts or target == item_id:
                    errors.append({"artifact_id": item_id, "code": "invalid_link"})
            for ref in artifact["references"]:
                row = record_by_id.get(ref["record_id"])
                if not row or str(row["root_id"]) != ref["root_id"]:
                    errors.append({"artifact_id": item_id, "code": "invalid_reference"})
                elif ref["root_id"] in deleted_roots:
                    errors.append({"artifact_id": item_id, "code": "deleted_reference"})
        base = self.active(repo, user)
        base_states = {
            d["item_id"]: json.loads(d["data"])
            for d in repo.find("details", version_id=base["id"], kind="record_state")
        }
        old_deleted = {
            str(r["root_id"])
            for r in inputs
            if base_states.get(str(r["id"]), {}).get("status") == "deleted"
        }
        restored = set()
        for row in inputs:
            data = json.loads(row["data"])
            if row["item_type"] == "command" and row["id"] > version["cursor"]:
                command = data["command"]
                if command["command_type"] == "restore" and (
                    states.get(str(row["id"]), {}).get("processing_state") == "applied"
                ):
                    restored.update(command["target_root_ids"])
        revived = (old_deleted & active_roots.keys()) | (old_deleted - deleted_roots)
        for root in revived - restored:
            errors.append({"root_id": root, "code": "restore_intent_required"})
        from .graph import Graph

        errors.extend(Graph(self).validate(repo, user, version))
        if base["id"] != version["base_id"]:
            errors.append({"code": "base_changed"})
        if errors:
            raise Error(
                422, "commit_invalid", "Fix the draft before committing", details=errors[:100]
            )
        repo.update("versions", {"id": base["id"]}, state="inactive")
        repo.update(
            "versions",
            {"id": dream_id},
            state="active",
            completed_at=now(),
            view_token=identifier(),
        )
        # Keep cursor as the input start until progress has been computed for the response.
        result, _ = self.dream_info(repo, user, dream_id)
        repo.update(
            "versions",
            {"id": dream_id},
            cursor=version["cutoff"],
            report=encode(result),
        )
        self.apply_states(repo, user, states)
        self.emit(repo, user, "context.published", {"dream_id": dream_id})
        self.prune(repo, user, dream_id)
        return result, self.dream_etag(version)

    @staticmethod
    def apply_states(repo, user, states):
        for row in repo.records(user):
            state = states.get(
                str(row["id"]),
                {
                    "status": "pending" if row["item_type"] == "context" else None,
                    "processing_state": "pending",
                    "reason": None,
                },
            )
            repo.update("records", {"id": row["id"], "user_id": user}, **state)

    def prune(self, repo, user, current_id):
        keep = set()
        current = repo.one("versions", id=current_id, user_id=user)
        for _ in range(self.settings.history_limit):
            if not current or not current["retained"]:
                break
            keep.add(current["id"])
            current = repo.one("versions", id=current["base_id"], user_id=user)
        for version in repo.find("versions", user_id=user):
            if version["id"] not in keep and version["state"] != "draft":
                repo.delete("details", version_id=version["id"])
                repo.update("versions", {"id": version["id"]}, retained=0)

    def abort(self, repo, user, dream_id):
        version = self.dream(repo, user, dream_id)
        if version["state"] == "aborted":
            return self.dream_info(repo, user, dream_id)
        if version["state"] not in {"draft", "aborted"}:
            raise Error(409, "dream_closed", "Cannot abort a committed dream")
        repo.update("versions", {"id": dream_id}, state="aborted", completed_at=now())
        result, tag = self.dream_info(repo, user, dream_id)
        repo.update("versions", {"id": dream_id}, report=encode(result))
        return result, tag

    def rollback(self, repo, user, token):
        if repo.one("versions", user_id=user, state="draft"):
            raise Error(409, "dream_exists", "Finish or abort the existing dream first")
        active = self.active(repo, user)
        if token != active["view_token"]:
            raise Error(412, "precondition_failed", "Read the current injection view first")
        previous = repo.one("versions", id=active["base_id"], user_id=user)
        if not previous or not previous["retained"]:
            raise Error(409, "no_history", "No retained previous publication")
        repo.update("versions", {"id": active["id"]}, state="rolled_back")
        new_token = identifier()
        repo.update("versions", {"id": previous["id"]}, state="active", view_token=new_token)
        states = {
            d["item_id"]: json.loads(d["data"])
            for d in repo.find("details", version_id=previous["id"], kind="record_state")
        }
        self.apply_states(repo, user, states)
        self.emit(repo, user, "context.rolled_back", {"view_token": new_token})
        return {
            "state": "rolled_back",
            "view_token": new_token,
            "after_id": str(previous["cursor"]),
            "through_id": str(repo.head(user)),
        }

    def injection(self, repo, user):
        version, cutoff, token = self.view(repo, user)
        cursor = version["cursor"] if version else 0
        index, _ = self.artifact(repo, user, "_index")
        summary = {key: index[key] for key in ("id", "content", "links")}
        pending_count, recent = repo.pending(
            user,
            cursor,
            cutoff,
            self.settings.buffer_max_items,
        )
        items = []
        for row in recent:
            data = json.loads(row["data"])
            text = encode(data["context"] if row["item_type"] == "context" else data["command"])
            items.append(
                {
                    "id": str(row["id"]),
                    "root_id": str(row["root_id"]),
                    "item_type": row["item_type"],
                    "produced_at": row["produced_at"],
                    "created_at": row["created_at"],
                    "preview": text[: self.settings.preview_max_chars],
                    "truncated": len(text) > self.settings.preview_max_chars,
                    "pending": True,
                }
            )
        data = {
            "summary": summary,
            "buffer": items,
            "view_token": token,
            "handled_through_id": str(cursor),
            "visible_through_id": str(cutoff),
            "pending_count": pending_count,
            "omitted_count": pending_count - len(items),
            "truncated": False,
        }
        while True:
            data["omitted_count"] = pending_count - len(items)
            data["truncated"] = bool(data["omitted_count"]) or any(i["truncated"] for i in items)
            if len(encode(data)) <= self.settings.injection_max_chars:
                return data
            if not items:
                raise Error(503, "injection_limit", "Stored index exceeds injection budget")
            items.pop(0)

    @staticmethod
    def emit(repo, user, kind, data):
        repo.insert("context_events", user_id=user, kind=kind, data=encode(data), created_at=now())

    def events(self, repo, user, after, limit):
        rows = repo.events(user, after, limit + 1)
        items = [
            {
                "id": str(row["id"]),
                "type": row["kind"],
                "data": json.loads(row["data"]),
                "created_at": row["created_at"],
            }
            for row in rows[:limit]
        ]
        return {
            "items": items,
            "next_after": items[-1]["id"] if items else str(after),
            "has_more": len(rows) > limit,
            "delivery": "at_least_once",
        }

    def assembly(self, repo, user):
        version, _, token = self.view(repo, user)
        detail = self.detail(repo, version, "artifact", "_core") if version else None
        content = json.loads(detail["data"])["content"] if detail else ""
        return {
            "protocol_version": "1",
            "stable": {"content": content, "cache_key": fingerprint(content)},
            "dynamic": self.injection(repo, user),
            "view_token": token,
            "events_url": "/v1/events",
        }
