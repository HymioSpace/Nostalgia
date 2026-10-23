import json
from datetime import datetime

from ..protocol import Error, encode, fingerprint


class Queries:
    def __init__(self, core):
        self.core = core

    @staticmethod
    def time_matches(item, filters):
        for prefix, name in (("produced", "produced_at"), ("created", "created_at")):
            for suffix, comparison in (
                ("after", lambda a, b: a >= b),
                ("before", lambda a, b: a <= b),
            ):
                boundary = filters.get(f"{prefix}_{suffix}")
                if boundary:
                    value = item.get(name)
                    if not value or not comparison(
                        datetime.fromisoformat(value.replace("Z", "+00:00")),
                        datetime.fromisoformat(boundary.replace("Z", "+00:00")),
                    ):
                        return False
        return True

    def record_matches(self, item, filters, include_deleted):
        if item["status"] == "deleted" and not include_deleted:
            return False
        for field in ("root_id", "item_type", "context_type", "status", "processing_state"):
            if filters.get(field) is not None and item.get(field) != filters[field]:
                return False
        source = item.get("source") or {}
        if filters.get("source_kind") and source.get("kind", "legacy") != filters["source_kind"]:
            return False
        for field in ("namespace", "external_id"):
            if filters.get(field) is not None and source.get(field) != filters[field]:
                return False
        if filters.get("ids") and item["id"] not in filters["ids"]:
            return False
        return self.time_matches(item, filters)

    def paginate(self, repo, user, request, items_factory, dream_id=None):
        if request["limit"] > self.core.settings.max_page_size:
            raise Error(400, "page_limit", "Page size exceeds configured maximum")
        version, head, view_token = self.core.view(repo, user, dream_id)
        query_hash = fingerprint({k: v for k, v in request.items() if k not in {"cursor", "limit"}})
        token = request.get("cursor")
        offset = 0
        if token:
            page = self.core.store.read_cursor(token)
            if page.get("user") != user or page.get("query") != query_hash:
                raise Error(400, "cursor_query", "Cursor belongs to another query")
            if page.get("view") != view_token:
                raise Error(409, "view_changed", "Restart pagination after the view changed")
            head, offset = page["head"], page["offset"]
        items = items_factory(version, head)
        selected = items[offset : offset + request["limit"]]
        next_cursor = None
        if offset + len(selected) < len(items):
            next_cursor = self.core.store.cursor(
                {
                    "user": user,
                    "query": query_hash,
                    "view": view_token,
                    "head": head,
                    "offset": offset + len(selected),
                }
            )
        return {"items": selected, "next_cursor": next_cursor, "visible_through_id": str(head)}

    def records(self, repo, user, query):
        request = query.model_dump(mode="json")

        def collect(version, head):
            cutoff = min(head, int(query.through_id)) if query.through_id else head
            result = []
            for row in repo.records(user, cutoff, after=int(query.after_id), root_id=query.root_id):
                item = self.core.record_view(repo, row, version)
                if self.record_matches(
                    item, request, query.include_deleted or bool(query.dream_id)
                ):
                    result.append(item)
            return result

        return self.paginate(repo, user, request, collect, query.dream_id)

    def search(self, repo, user, query):
        if query.mode == "semantic":
            raise Error(400, "unsupported_search", "This service supports filter and text search")
        if query.mode == "text" and not query.query:
            raise Error(400, "query_required", "Text search requires a nonempty query")
        request = query.model_dump(mode="json")
        filters = query.filters.model_dump(mode="json")
        record_only = {
            "namespace",
            "external_id",
            "source_kind",
            "item_type",
            "context_type",
            "processing_state",
        }
        artifact_only = {"topic", "artifact_type"}
        if query.target == "records" and any(filters.get(k) for k in artifact_only):
            raise Error(400, "invalid_filter", "Artifact-only filter used for records")
        if query.target == "artifacts" and any(filters.get(k) for k in record_only):
            raise Error(400, "invalid_filter", "Record-only filter used for artifacts")

        def text_matches(content):
            return query.mode != "text" or query.query.casefold() in encode(content).casefold()

        def collect(version, head):
            result = []
            if query.target in {"records", "all"} and not any(
                filters.get(k) for k in artifact_only
            ):
                for row in repo.records(user, head):
                    item = self.core.record_view(repo, row, version)
                    if self.record_matches(
                        item, filters, query.include_deleted or bool(query.dream_id)
                    ) and text_matches(item.get("context") or item.get("command")):
                        result.append(self.hit("record", item))
            if (
                version
                and query.target in {"artifacts", "all"}
                and not any(filters.get(k) for k in record_only)
            ):
                for detail in sorted(
                    repo.find("details", version_id=version["id"], kind="artifact"),
                    key=lambda d: d["item_id"],
                ):
                    item = {
                        "id": detail["item_id"],
                        **json.loads(detail["data"]),
                        "status": "active",
                        "created_at": detail["created_at"],
                        "updated_at": detail["updated_at"],
                    }
                    if filters["ids"] and item["id"] not in filters["ids"]:
                        continue
                    if filters["root_id"] and not any(
                        r["root_id"] == filters["root_id"] for r in item["references"]
                    ):
                        continue
                    if filters["topic"] and filters["topic"] not in item["topics"]:
                        continue
                    if (
                        filters["artifact_type"]
                        and filters["artifact_type"] != item["artifact_type"]
                    ):
                        continue
                    if filters["status"] and filters["status"] != "active":
                        continue
                    if not self.time_matches(item, filters):
                        continue
                    if text_matches(
                        {
                            "content": item["content"],
                            "topics": item["topics"],
                            "fields": item["fields"],
                        }
                    ):
                        result.append(self.hit("artifact", item))
            return result

        data = self.paginate(repo, user, request, collect, query.dream_id)
        data.update(
            {
                "index_through_id": data["visible_through_id"],
                "partial": False,
                "search_method": "authoritative_scan",
            }
        )
        return data

    def hit(self, kind, item):
        content = (
            item.get("content")
            if kind == "artifact"
            else (item.get("context") or item.get("command"))
        )
        text = content if isinstance(content, str) else encode(content)
        return {
            "kind": kind,
            "id": item["id"],
            "root_id": item.get("root_id"),
            "preview": text[: self.core.settings.preview_max_chars],
            "truncated": len(text) > self.core.settings.preview_max_chars,
            "status": item["status"],
            "processing_state": item.get("processing_state"),
            "source": item.get("source"),
            "references": item.get("references", []),
            "topics": item.get("topics", []),
            "created_at": item["created_at"],
        }
