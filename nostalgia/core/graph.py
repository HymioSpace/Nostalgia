"""Versioned arbitrary card relations; no inferred edges in read projections."""

import json

from ..protocol import Error, fingerprint
from ..protocol.graph import GraphQuery


def edge_id(value):
    return "rel-" + fingerprint([value["source"], value["target"], value["type"]])[:24]


class Graph:
    def __init__(self, core):
        self.core = core

    def put(self, repo, user, dream_id, relation, match):
        version = self.core.dream(repo, user, dream_id, editable=True)
        self.core.check_match(self.core.dream_etag(version), match)
        value = relation.model_dump(mode="json")
        item_id = edge_id(value)
        self.core.save_detail(repo, version, "relation", item_id, value)
        # A relation edit does not invalidate an already confirmed navigation index.
        self.core.bump(repo, version, index_ready=bool(version["index_ready"]))
        return {"id": item_id, **value}, self.core.dream_info(repo, user, dream_id)[1]

    def materialize(self, repo, user, version, cutoff):
        nodes, edges = {}, {}
        if not version:
            return nodes, edges
        for row in repo.records(user, cutoff):
            value = self.core.record_view(repo, row, version)
            if value["status"] == "deleted" or value["item_type"] != "context":
                continue
            context = value.get("context") or {}
            text = context.get("text", "")
            key = f"record:{value['id']}"
            nodes[key] = {
                "key": key,
                "id": value["id"],
                "kind": "record",
                "revision": value["id"],
                "title": context.get("title") or str(text).split("\n")[0][:48],
                "preview": str(text)[:500],
                "source": value["source"],
                "category": None,
                "status": value["status"],
                "updated_at": value["created_at"],
                "role": "original",
                "topics": [],
            }
        details = repo.find("details", version_id=version["id"])
        for row in details:
            if row["kind"] != "artifact" or row["item_id"] == "_index":
                continue
            value = json.loads(row["data"])
            fields = value.get("fields", {})
            key = f"artifact:{row['item_id']}"
            nodes[key] = {
                "key": key,
                "id": row["item_id"],
                "kind": "artifact",
                "revision": str(row["revision"]),
                "title": fields.get("title") or value["content"][:48],
                "preview": value["content"][:500],
                "source": None,
                "category": fields.get("category", "uncategorized"),
                "status": "active",
                "updated_at": row["updated_at"],
                "topics": value.get("topics", []),
                "domains": fields.get("domains", []),
                "labels": fields.get("labels", []),
                "role": "overview" if row["item_id"] == "_core" else "understanding",
            }
            for ref in value.get("references", []):
                edge = {
                    "source": {"kind": "record", "id": ref["record_id"]},
                    "target": {"kind": "artifact", "id": row["item_id"]},
                    "type": "supports",
                    "status": "active",
                    "origin": "import",
                    "rationale": "原始依据",
                    "evidence_refs": [ref],
                }
                edges[edge_id(edge)] = {
                    "id": edge_id(edge),
                    **edge,
                    "revision": str(row["revision"]),
                    "updated_at": row["updated_at"],
                    "legacy": True,
                }
            for target in value.get("links", []):
                edge = {
                    "source": {"kind": "artifact", "id": row["item_id"]},
                    "target": {"kind": "artifact", "id": target},
                    "type": "navigates",
                    "status": "active",
                    "origin": "import",
                    "rationale": "已有导航关联",
                    "evidence_refs": [],
                }
                edges[edge_id(edge)] = {
                    "id": edge_id(edge),
                    **edge,
                    "revision": str(row["revision"]),
                    "updated_at": row["updated_at"],
                    "legacy": True,
                }
        for row in details:
            if row["kind"] == "relation":
                value = json.loads(row["data"])
                edges[row["item_id"]] = {
                    "id": row["item_id"],
                    **value,
                    "revision": str(row["revision"]),
                    "updated_at": row["updated_at"],
                    "legacy": False,
                }
        return nodes, edges

    @staticmethod
    def endpoints(edge):
        return tuple(f"{edge[k]['kind']}:{edge[k]['id']}" for k in ("source", "target"))

    def validate(self, repo, user, version):
        nodes, edges = self.materialize(repo, user, version, version["cutoff"])
        errors, replacements = [], {}
        for edge in edges.values():
            if edge["legacy"] or edge["status"] == "retired":
                continue
            a, b = self.endpoints(edge)
            if a not in nodes or b not in nodes:
                errors.append({"relation_id": edge["id"], "code": "invalid_endpoint"})
            for ref in edge["evidence_refs"]:
                row = repo.one("records", user_id=user, id=int(ref["record_id"]))
                if (
                    not row
                    or str(row["root_id"]) != ref["root_id"]
                    or f"record:{ref['record_id']}" not in nodes
                ):
                    errors.append({"relation_id": edge["id"], "code": "invalid_relation_evidence"})
            if edge["type"] == "supersedes" and edge["status"] == "active":
                replacements.setdefault(a, []).append(b)

        def cycle(node, visiting, visited):
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            if any(cycle(n, visiting, visited) for n in replacements.get(node, [])):
                return True
            visiting.remove(node)
            visited.add(node)
            return False

        visited = set()
        if any(cycle(n, set(), visited) for n in replacements):
            errors.append({"code": "supersedes_cycle"})
        return errors

    def query(self, repo, user, query):
        version, cutoff, token = self.core.view(repo, user)
        snapshot = fingerprint([token, cutoff])
        if query.snapshot and query.snapshot != snapshot:
            raise Error(409, "graph_changed", "Context changed; reload the graph")
        nodes, edges = self.materialize(repo, user, version, cutoff)
        edges = {
            k: e
            for k, e in edges.items()
            if e["status"] != "retired" and all(n in nodes for n in self.endpoints(e))
        }
        previous = repo.one("versions", id=version["base_id"], user_id=user) if version else None
        old_nodes, old_edges = (
            self.materialize(repo, user, previous, previous["cutoff"])
            if previous and previous["retained"]
            else ({}, {})
        )
        for key, node in nodes.items():
            old = old_nodes.get(key)
            node["change"] = (
                "added" if not old else "updated" if old["revision"] != node["revision"] else ""
            )
            node["degree"] = sum(key in self.endpoints(e) for e in edges.values())
        candidates = [
            k
            for k, n in nodes.items()
            if (
                not query.query
                or query.query.casefold()
                in (
                    n["title"]
                    + n["preview"]
                    + " ".join(n.get("domains", []))
                    + " ".join(n.get("topics", []))
                ).casefold()
            )
            and (not query.changes_only or n["change"])
        ]
        candidates.sort(key=lambda k: (nodes[k]["updated_at"], k), reverse=True)
        if query.seed:
            seed = query.seed.key
            if seed not in nodes:
                raise Error(404, "not_found", "Card not available")
            selected, frontier = {seed}, {seed}
            for _ in range(query.depth):
                neighbors = {
                    n
                    for e in edges.values()
                    if frontier.intersection(self.endpoints(e))
                    for n in self.endpoints(e)
                } - selected
                selected.update(neighbors)
                frontier = neighbors
            candidates = [seed] + sorted(selected - {seed})
        total = len(candidates)
        chosen = candidates[query.offset : query.offset + query.limit]
        chosen_set = set(chosen)
        visible_edges = [e for e in edges.values() if set(self.endpoints(e)) <= chosen_set]
        for e in visible_edges:
            old = old_edges.get(e["id"])
            e["change"] = (
                "added" if not old else "updated" if old["revision"] != e["revision"] else ""
            )
        for key in chosen:
            nodes[key]["hidden_neighbors"] = sum(
                key in self.endpoints(e) and not set(self.endpoints(e)) <= chosen_set
                for e in edges.values()
            )
        return {
            "nodes": [nodes[k] for k in chosen],
            "edges": visible_edges,
            "snapshot": snapshot,
            "scope_count": total,
            "next_offset": query.offset + query.limit
            if query.offset + query.limit < total
            else None,
            "truncated": total > len(chosen),
            "history_available": bool(previous and previous["retained"]),
        }

    def retrieve(self, repo, user, query):
        overview = self.query(repo, user, GraphQuery(query=query.query, limit=min(5, query.limit)))
        nodes, edges = {}, {}
        for seed in overview["nodes"]:
            page = self.query(
                repo,
                user,
                GraphQuery(seed={"kind": seed["kind"], "id": seed["id"]}, limit=query.limit),
            )
            for node in page["nodes"]:
                if len(nodes) < query.limit:
                    nodes[node["key"]] = node
            edges.update({e["id"]: e for e in page["edges"]})
        return {
            "nodes": list(nodes.values()),
            "edges": [e for e in edges.values() if set(self.endpoints(e)) <= nodes.keys()],
            "snapshot": overview["snapshot"],
            "bounded": True,
            "method": "literal-plus-relations",
            "note": "Read full cards before using previews; related is not evidence.",
        }
