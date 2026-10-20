import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import TOKENS, Client
from fastapi.testclient import TestClient

from nostalgia.api import create_app
from nostalgia.protocol import encode


def refs(record):
    return [{"root_id": record["root_id"], "record_id": record["id"]}]


def test_authentication_schema_and_empty_store(api):
    assert api.client.get("/healthz").status_code == 200
    assert api.client.get("/v1/records").status_code == 401
    bad = api.client.get("/v1/records", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    assert bad.headers["www-authenticate"] == "Bearer"
    initial = api.get("/v1/injection").json()
    assert initial["summary"]["content"] == ""
    assert initial["buffer"] == []
    assert initial["handled_through_id"] == "0"
    assert api.get("/v1/capabilities").json()["search_modes"] == ["filter", "text"]
    schema = api.client.get("/openapi.json").json()
    assert "RecordCreate" in schema["components"]["schemas"]
    assert "/v1/dreams/{dream_id}/commit" in schema["paths"]
    assert api.post("/v1/search", json={"mode": "semantic"}).status_code == 400


def test_append_idempotency_and_source_lineage(api):
    payload = {
        "item_type": "context",
        "context_type": "text",
        "context": {"text": "old"},
        "source": {"namespace": "chrome/account1", "external_id": "url", "version": "v1"},
    }
    first = api.post("/v1/records", key="stable", json=payload)
    assert first.status_code == 201
    assert api.post("/v1/records", key="stable", json=payload).json() == first.json()
    assert api.post("/v1/records", json=payload).json()["id"] == first.json()["id"]
    changed = {**payload, "context": {"text": "new"}}
    assert api.post("/v1/records", key="stable", json=changed).status_code == 409
    assert api.post("/v1/records", json=changed).status_code == 412
    changed["source"] = {**payload["source"], "version": "v2"}
    assert api.post("/v1/records", json=changed).status_code == 412
    changed["expected_record_id"] = first.json()["id"]
    second = api.post("/v1/records", json=changed)
    assert second.status_code == 201, second.text
    assert second.json()["root_id"] == first.json()["id"]
    assert api.post("/v1/records", json=changed).json()["id"] == second.json()["id"]
    assert len(api.get("/v1/records", params={"root_id": first.json()["id"]}).json()["items"]) == 2
    other = {**payload, "source": {**payload["source"], "namespace": "chrome/account2"}}
    assert api.post("/v1/records", json=other).json()["root_id"] != first.json()["root_id"]


def test_draft_isolation_fixed_cutoff_and_atomic_publication(api):
    first = api.context("Hymio is personal")
    dream = api.begin()
    later = api.context("new during dreaming")
    assert api.get(f"/v1/records/{later['id']}", params={"dream_id": dream}).status_code == 404
    assert [
        r["id"] for r in api.get("/v1/records", params={"dream_id": dream}).json()["items"]
    ] == [
        first["id"],
    ]
    api.mark(dream, first)
    api.put(dream, "purpose", "Personal agent", references=refs(first), topics=["hymio"])
    assert api.get("/v1/artifacts/purpose").status_code == 404
    assert api.get(f"/v1/records/{first['id']}").json()["status"] == "pending"
    found = api.post(
        "/v1/search",
        json={"target": "artifacts", "dream_id": dream, "filters": {"root_id": first["root_id"]}},
    ).json()
    assert [r["id"] for r in found["items"]] == ["purpose"]
    api.index(dream, "Hymio: personal agent", links=["purpose"])
    old = api.get("/v1/injection").json()
    assert old["summary"]["content"] == ""
    assert old["pending_count"] == 2
    result = api.commit(dream)
    assert result.status_code == 200, result.text
    new = api.get("/v1/injection").json()
    assert new["handled_through_id"] == first["id"]
    assert new["summary"]["links"] == ["purpose"]
    assert [r["id"] for r in new["buffer"]] == [later["id"]]
    assert api.get(f"/v1/records/{first['id']}").json()["status"] == "active"
    info = api.get(f"/v1/dreams/{dream}").json()
    assert info["input_count"] == info["handled_count"] == 1
    assert info["after_id"] == "0"
    assert api.commit(dream).status_code == 200
    assert api.post(f"/v1/dreams/{dream}/abort").status_code == 409


def test_commit_validation_and_optimistic_concurrency(api):
    record = api.context("a")
    dream = api.begin()
    assert api.post("/v1/dreams").status_code == 409
    invalid = api.commit(dream)
    assert invalid.status_code == 422
    codes = {d["code"] for d in invalid.json()["error"]["details"]}
    assert codes == {"unhandled_input", "index_not_confirmed"}
    stale_dream_etag = api.get(f"/v1/dreams/{dream}").headers["etag"]
    api.mark(dream, record)
    path = f"/v1/artifacts/a?dream_id={dream}"
    missing = api.request("PUT", path, json={"content": "a", "references": refs(record)})
    assert missing.status_code == 412
    old = api.put(dream, "a", "a", references=refs(record))
    updated = api.put(dream, "a", "b", references=refs(record))
    assert old.headers["etag"] != updated.headers["etag"]
    stale = api.request(
        "PUT",
        path,
        json={"content": "c", "references": refs(record)},
        headers={"If-Match": old.headers["etag"]},
    )
    assert stale.status_code == 412
    api.index(dream, links=["a"])
    assert (
        api.post(
            f"/v1/dreams/{dream}/commit",
            headers={
                "If-Match": stale_dream_etag,
            },
        ).status_code
        == 412
    )
    api.put(dream, "a", "after index", references=refs(record))
    assert api.commit(dream).status_code == 422
    api.index(dream, links=["missing"])
    assert api.commit(dream).status_code == 422
    api.index(dream, links=["a"])
    assert api.commit(dream).status_code == 200


def test_delete_is_deferred_requires_repair_and_preserves_original(api):
    record = api.context("original")
    api.publish(record, "current")
    command = api.command("delete original", command_type="delete", roots=[record["root_id"]])
    assert api.get("/v1/artifacts/note").status_code == 200
    assert api.get(f"/v1/records/{record['id']}").json()["status"] == "active"
    dream = api.begin()
    api.mark(dream, command)
    api.mark(dream, record, status="deleted")
    assert api.get(f"/v1/records/{record['id']}").json()["status"] == "active"
    api.index(dream, "deleted", links=["note"])
    failed = api.commit(dream)
    assert failed.status_code == 422
    assert "deleted_reference" in {d["code"] for d in failed.json()["error"]["details"]}
    artifact = api.get(f"/v1/artifacts/note?dream_id={dream}")
    delete = api.request(
        "DELETE",
        f"/v1/artifacts/note?dream_id={dream}",
        headers={"If-Match": artifact.headers["etag"]},
    )
    assert delete.status_code == 204
    api.index(dream, "deleted")
    assert api.commit(dream).status_code == 200
    assert api.get("/v1/artifacts/note").status_code == 404
    assert api.get("/v1/records").json()["items"] == [
        api.get(f"/v1/records/{command['id']}").json(),
    ]
    original = api.get(f"/v1/records/{record['id']}").json()
    assert original["context"] == {"text": "original"}
    assert original["status"] == "deleted"
    assert (
        len(
            api.get(
                "/v1/records", params={"root_id": record["root_id"], "include_deleted": True}
            ).json()["items"]
        )
        == 1
    )
    assert (
        api.post(
            "/v1/search", json={"mode": "text", "query": "original", "target": "artifacts"}
        ).json()["items"]
        == []
    )


def test_unknown_command_needs_input_survives_cursor_and_buffer(api):
    command = api.command("delete that one", command_type="future-command")
    dream = api.begin()
    api.mark(dream, command, processing="needs_input", reason="Which one?")
    api.index(dream, "One request needs clarification")
    assert api.commit(dream).status_code == 200
    assert api.get("/v1/injection").json()["buffer"] == []
    pending = api.get("/v1/records", params={"processing_state": "needs_input"}).json()
    assert [r["id"] for r in pending["items"]] == [command["id"]]
    assert api.get(f"/v1/dreams/{dream}").json()["needs_input_count"] == 1


def test_buffer_fifo_time_order_truncation_and_full_input(custom_api):
    api, settings = custom_api(
        buffer_max_items=2, preview_max_chars=8, index_max_chars=512, injection_max_chars=1536
    )
    first = api.context("first")
    old = api.context("很久以前的资料" * 30, produced_at="2000-01-01T00:00:00Z")
    newest = api.command("a request" * 100)
    injection = api.get("/v1/injection").json()
    assert [i["id"] for i in injection["buffer"]] == [old["id"], newest["id"]]
    assert injection["omitted_count"] == 1
    assert injection["truncated"]
    assert len(encode(injection)) <= settings.injection_max_chars
    assert all(len(i["preview"]) <= 8 for i in injection["buffer"])
    assert api.get(f"/v1/records/{first['id']}").status_code == 200
    dream = api.begin()
    all_inputs = api.get("/v1/records", params={"dream_id": dream}).json()["items"]
    assert len(all_inputs) == 3


def test_pagination_complete_reverse_references_and_view_changes(api):
    record = api.context("source")
    dream = api.begin()
    api.mark(dream, record)
    for i in range(5):
        api.put(dream, f"card{i}", f"content{i}", references=refs(record))
    api.index(dream, links=["card0"])
    assert api.commit(dream).status_code == 200
    query = {"target": "artifacts", "filters": {"root_id": record["root_id"]}, "limit": 2}
    found, cursor = [], None
    while True:
        page = api.post("/v1/search", json={**query, "cursor": cursor}).json()
        found.extend(r["id"] for r in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert found == [f"card{i}" for i in range(5)]
    api.context("second")
    api.context("third")
    first = api.get("/v1/records", params={"limit": 1}).json()
    api.context("fourth after snapshot")
    ids = [i["id"] for i in first["items"]]
    cursor = first["next_cursor"]
    while cursor:
        page = api.get("/v1/records", params={"limit": 1, "cursor": cursor}).json()
        ids.extend(i["id"] for i in page["items"])
        cursor = page["next_cursor"]
    assert len(ids) == 3
    tampered = api.get("/v1/records", params={"cursor": first["next_cursor"] + "x"})
    assert tampered.status_code == 400
    dream2 = api.begin()
    for row in api.get("/v1/records", params={"after_id": record["id"]}).json()["items"]:
        api.mark(dream2, row)
    api.index(dream2, links=["card0"])
    assert api.commit(dream2).status_code == 200
    changed = api.get("/v1/records", params={"limit": 1, "cursor": first["next_cursor"]})
    assert changed.status_code == 409


def test_restart_draft_recovery_idempotency_and_rollback(api, settings):
    first = api.context("first")
    api.publish(first, "first summary")
    command = api.command("update summary")
    dream = api.begin()
    api.mark(dream, command)
    api.put(dream, "note", "second", references=refs(first))
    api.index(dream, "second summary", links=["note"])
    with TestClient(create_app(settings)) as raw:
        restarted = Client(raw)
        assert restarted.get(f"/v1/dreams/{dream}").json()["handled_count"] == 1
        assert restarted.get("/v1/injection").json()["summary"]["content"] == "first summary"
        result = restarted.commit(dream, key="commit-once")
        assert result.status_code == 200, result.text
        current = restarted.get("/v1/injection").json()
        assert current["summary"]["content"] == "second summary"
        response = restarted.post(
            "/v1/dreams/rollback", key="rollback-once", json={"view_token": current["view_token"]}
        )
        assert response.status_code == 200
        assert (
            restarted.post(
                "/v1/dreams/rollback",
                key="rollback-once",
                json={"view_token": current["view_token"]},
            ).json()
            == response.json()
        )
        restored = restarted.get("/v1/injection").json()
        assert restored["summary"]["content"] == "first summary"
        assert [r["id"] for r in restored["buffer"]] == [command["id"]]
        assert restarted.get(f"/v1/records/{command['id']}").json()["processing_state"] == "pending"
        assert (
            restarted.post(
                "/v1/dreams/rollback", json={"view_token": current["view_token"]}
            ).status_code
            == 412
        )
        assert restarted.get(f"/v1/records/{first['id']}").json()["status"] == "active"


def test_abandon_and_late_tools_cannot_mutate_published_data(api):
    record = api.context("raw")
    dream = api.begin()
    response = api.mark(dream, record)
    api.put(dream, "a", "draft", references=refs(record))
    assert api.post(f"/v1/dreams/{dream}/abort").status_code == 200
    assert api.post(f"/v1/dreams/{dream}/abort").status_code == 200
    assert api.commit(dream).status_code == 409
    late = api.request(
        "PATCH",
        f"/v1/records/{record['id']}?dream_id={dream}",
        headers={"If-Match": response.headers["etag"]},
        json={"processing_state": "applied", "status": "active"},
    )
    assert late.status_code == 409
    assert api.get("/v1/artifacts/a").status_code == 404
    assert api.get(f"/v1/records/{record['id']}").json()["status"] == "pending"
    assert api.get("/v1/injection").json()["pending_count"] == 1


def test_cross_user_records_dreams_references_assets_and_cursors(api):
    record = api.context("private")
    dream = api.begin()
    asset = api.post(
        "/v1/assets",
        content=b"private file",
        headers={
            "Content-Type": "application/octet-stream",
        },
    ).json()
    api.context("another")
    cursor = api.get("/v1/records", params={"limit": 1}).json()["next_cursor"]
    bob = Client(api.client, "bob")
    for path in (f"/v1/records/{record['id']}", f"/v1/dreams/{dream}", f"/v1/assets/{asset['id']}"):
        assert bob.get(path).status_code == 404
    assert bob.get("/v1/records").json()["items"] == []
    assert bob.get("/v1/records", params={"limit": 1, "cursor": cursor}).status_code == 400
    own = bob.context("own")
    own_dream = bob.begin()
    bob.mark(own_dream, own)
    invalid = bob.request(
        "PUT",
        f"/v1/artifacts/stolen?dream_id={own_dream}",
        headers={"If-None-Match": "*"},
        json={"content": "stolen", "references": refs(record)},
    )
    assert invalid.status_code == 422
    bad_asset = bob.post(
        "/v1/records",
        json={
            "item_type": "context",
            "context_type": "file",
            "context": {},
            "asset_ids": [asset["id"]],
        },
    )
    assert bad_asset.status_code == 422


def test_binary_asset_roundtrip_and_limits(custom_api):
    api, _ = custom_api(max_asset_bytes=32, max_json_bytes=256)
    data = bytes(range(32))
    response = api.post(
        "/v1/assets",
        content=data,
        key="upload",
        headers={
            "Content-Type": "application/octet-stream",
        },
    )
    assert response.status_code == 201
    assert api.get(f"/v1/assets/{response.json()['id']}").content == data
    assert (
        api.post(
            "/v1/assets",
            content=data,
            key="upload",
            headers={
                "Content-Type": "application/octet-stream",
            },
        ).json()
        == response.json()
    )
    assert api.post("/v1/assets", content=data + b"x").status_code == 413
    assert api.post("/v1/records", json={"huge": "x" * 256}).status_code == 413


def test_transaction_failure_does_not_publish_partial_state(api, monkeypatch):
    record = api.context("raw")
    dream = api.begin()
    api.mark(dream, record)
    api.put(dream, "a", "ready", references=refs(record))
    api.index(dream, "new summary", links=["a"])
    original = api.client.app.state.core.prune

    def fail(*_args):
        raise RuntimeError("simulated crash before transaction commit")

    monkeypatch.setattr(api.client.app.state.core, "prune", fail)
    with pytest.raises(RuntimeError, match="simulated crash"):
        api.commit(dream, key="commit")
    assert api.get("/v1/artifacts/a").status_code == 404
    assert api.get(f"/v1/records/{record['id']}").json()["status"] == "pending"
    assert api.get("/v1/injection").json()["handled_through_id"] == "0"
    assert api.get(f"/v1/dreams/{dream}").json()["state"] == "draft"
    monkeypatch.setattr(api.client.app.state.core, "prune", original)
    assert api.commit(dream, key="commit").status_code == 200


def test_concurrent_append_begin_and_atomic_injection(api):
    first = api.context("initial")
    api.publish(first, "old")

    def append(i):
        return api.context(f"incoming {i}")["id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(append, i) for i in range(24)]
        begin_future = pool.submit(api.begin)
        ids = [f.result() for f in futures]
        dream = begin_future.result()
    info = api.get(f"/v1/dreams/{dream}").json()
    inputs = api.get(
        "/v1/records", params={"dream_id": dream, "after_id": first["id"], "limit": 100}
    ).json()["items"]
    assert {r["id"] for r in inputs} == {i for i in ids if int(i) <= int(info["through_id"])}
    for record in inputs:
        api.mark(dream, record)
    api.index(dream, "new", links=["note"])

    def read_injection(_):
        data = api.get("/v1/injection").json()
        if data["summary"]["content"] == "old":
            assert data["handled_through_id"] == first["id"]
        else:
            assert data["summary"]["content"] == "new"
            assert data["handled_through_id"] == info["through_id"]
        assert all(int(i["id"]) > int(data["handled_through_id"]) for i in data["buffer"])
        return data

    with ThreadPoolExecutor(max_workers=8) as pool:
        reads = [pool.submit(read_injection, i) for i in range(30)]
        commit = pool.submit(api.commit, dream)
        assert commit.result().status_code == 200
        for future in reads:
            future.result()


def test_invalid_payloads_are_protocol_errors(api):
    for payload in (
        {"item_type": "context", "context_type": "text"},
        {
            "item_type": "command",
            "context": {},
            "command": {"command_type": "x", "command_content": "x"},
        },
        {"item_type": "context", "context_type": "text", "context": {}, "user_id": "bob"},
        {
            "item_type": "context",
            "context_type": "text",
            "context": {},
            "produced_at": "2026-10-20T00:00:00",
        },
    ):
        response = api.post("/v1/records", json=payload)
        assert response.status_code == 400, response.text
        assert "error" in response.json()
    response = api.client.post(
        "/v1/records",
        headers={"Authorization": f"Bearer {TOKENS['alice']}"},
        json={"item_type": "context", "context_type": "text", "context": {}},
    )
    assert response.status_code == 400
    assert api.get("/v1/records/not-a-number").status_code == 400
    assert (
        api.post(
            "/v1/records",
            content=json.dumps({}),
            headers={
                "Content-Type": "text/plain",
            },
        ).status_code
        == 415
    )
