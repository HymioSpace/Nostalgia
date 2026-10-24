from conftest import Client
from fastapi.testclient import TestClient

from nostalgia.api import create_app


def reference(record):
    return [{"root_id": record["root_id"], "record_id": record["id"]}]


def test_old_and_new_source_states_and_many_to_many_trace(api):
    first = api.context("version one")
    other = api.context("other source")
    dream = api.begin()
    api.mark(dream, first)
    api.mark(dream, other)
    api.put(dream, "joined", "combined", references=reference(first) + reference(other))
    api.index(dream, links=["joined"])
    assert api.commit(dream).status_code == 200
    first_card = api.get("/v1/artifacts/joined").json()
    changed = api.context("version two", root_id=first["root_id"], expected_record_id=first["id"])
    dream = api.begin()
    api.mark(dream, changed)
    api.put(dream, "joined", "updated combined", references=reference(changed) + reference(other))
    api.index(dream, links=["joined"])
    invalid = api.commit(dream)
    assert invalid.status_code == 422
    assert "multiple_active_records" in {d["code"] for d in invalid.json()["error"]["details"]}
    api.mark(dream, first, status="obsolete")
    api.index(dream, links=["joined"])
    assert api.commit(dream).status_code == 200
    rows = api.get("/v1/records", params={"root_id": first["root_id"]}).json()["items"]
    assert [(r["id"], r["status"]) for r in rows] == [
        (first["id"], "obsolete"),
        (changed["id"], "active"),
    ]
    for root in (first["root_id"], other["root_id"]):
        results = api.post(
            "/v1/search", json={"target": "artifacts", "filters": {"root_id": root}}
        ).json()["items"]
        assert [r["id"] for r in results] == ["joined"]
    card = api.get("/v1/artifacts/joined").json()
    assert card["created_at"] == first_card["created_at"]
    assert card["updated_at"] >= first_card["updated_at"]
    assert card["references"][0]["record_id"] == changed["id"]


def test_deleted_root_does_not_restore_without_explicit_intent(api):
    original = api.context("a preference")
    api.publish(original)
    delete = api.command("forget preference", "delete", [original["root_id"]])
    dream = api.begin()
    api.mark(dream, delete)
    api.mark(dream, original, status="deleted")
    card = api.get(f"/v1/artifacts/note?dream_id={dream}")
    assert (
        api.request(
            "DELETE",
            f"/v1/artifacts/note?dream_id={dream}",
            headers={
                "If-Match": card.headers["etag"],
            },
        ).status_code
        == 204
    )
    api.index(dream, "forgotten")
    assert api.commit(dream).status_code == 200
    newer = api.context(
        "synchronized again", root_id=original["root_id"], expected_record_id=original["id"]
    )
    dream = api.begin()
    api.mark(dream, original, status="obsolete")
    api.mark(dream, newer)
    api.put(dream, "restored", "preference", references=reference(newer))
    api.index(dream, "preference", links=["restored"])
    failed = api.commit(dream)
    assert failed.status_code == 422
    assert "restore_intent_required" in {d["code"] for d in failed.json()["error"]["details"]}
    api.mark(dream, newer, status="obsolete")
    api.index(dream, "still references a forgotten root", links=["restored"])
    no_active_record = api.commit(dream)
    assert no_active_record.status_code == 422
    assert "restore_intent_required" in {
        d["code"] for d in no_active_record.json()["error"]["details"]
    }
    restore = api.command("restore preference", "restore", [original["root_id"]])
    assert api.get(f"/v1/records/{restore['id']}?dream_id={dream}").status_code == 404
    assert api.post(f"/v1/dreams/{dream}/abort").status_code == 200
    dream = api.begin()
    api.mark(dream, original, status="obsolete")
    api.mark(dream, newer)
    api.mark(dream, restore)
    api.put(dream, "restored", "preference", references=reference(newer))
    api.index(dream, "preference", links=["restored"])
    assert api.commit(dream).status_code == 200


def test_retention_bounds_snapshots_but_keeps_logs_and_terminal_receipts(custom_api):
    api, _ = custom_api(history_limit=2)
    first = api.context("original")
    first_dream = api.publish(first, "v1")
    original_terminal = api.get(f"/v1/dreams/{first_dream}").json()
    for i in range(2, 5):
        command = api.command(f"rewrite to v{i}")
        dream = api.begin()
        api.mark(dream, command)
        api.put(dream, "note", f"v{i}", references=reference(first))
        api.index(dream, f"v{i}", links=["note"])
        assert api.commit(dream).status_code == 200
    expired = api.get(f"/v1/dreams/{first_dream}").json()
    assert expired["state"] == "committed"
    assert expired["handled_count"] == original_terminal["handled_count"]
    assert not expired["snapshot_retained"]
    assert api.commit(first_dream).status_code == 200
    assert api.get(f"/v1/records/{first['id']}").status_code == 200
    view = api.get("/v1/injection").json()
    assert (
        api.post("/v1/dreams/rollback", json={"view_token": view["view_token"]}).status_code == 200
    )
    assert api.get("/v1/injection").json()["summary"]["content"] == "v3"
    view = api.get("/v1/injection").json()
    assert (
        api.post("/v1/dreams/rollback", json={"view_token": view["view_token"]}).status_code == 409
    )


def test_rollback_with_active_draft_and_idempotency_across_restart(api, settings):
    record = api.context("content")
    api.publish(record)
    view = api.get("/v1/injection").json()
    dream = api.begin()
    assert (
        api.post("/v1/dreams/rollback", json={"view_token": view["view_token"]}).status_code == 409
    )
    first = api.post(f"/v1/dreams/{dream}/abort", key="abort-once")
    with TestClient(create_app(settings)) as raw:
        restarted = Client(raw)
        assert restarted.post(f"/v1/dreams/{dream}/abort", key="abort-once").json() == first.json()
        assert restarted.post(f"/v1/dreams/{dream}/abort").json() == first.json()
    assert (
        api.post("/v1/dreams/rollback", json={"view_token": view["view_token"]}).status_code == 200
    )
    restored = api.get("/v1/injection").json()
    assert restored["summary"]["content"] == ""
    assert restored["handled_through_id"] == "0"
    assert api.get(f"/v1/records/{record['id']}").json()["status"] == "pending"


def test_search_time_filters_chinese_and_json_fields(api):
    old = api.context("过去的项目", produced_at="2020-01-01T08:00:00+08:00")
    api.context("CURRENT project", produced_at="2026-10-20T00:00:00Z")
    matches = api.post("/v1/search", json={"mode": "text", "query": "过去", "target": "records"})
    assert [r["id"] for r in matches.json()["items"]] == [old["id"]]
    matches = api.post(
        "/v1/search",
        json={
            "mode": "text",
            "query": "current",
            "target": "records",
            "filters": {"produced_after": "2026-01-01T00:00:00Z"},
        },
    )
    assert len(matches.json()["items"]) == 1
    assert matches.json()["partial"] is False


def test_protocol_integer_limits_and_invalid_reference(api):
    huge = str(2**63)
    assert api.get(f"/v1/records/{huge}").status_code == 400
    assert api.get("/v1/records", params={"root_id": huge}).status_code == 400
    assert (
        api.post(
            "/v1/records",
            json={
                "item_type": "context",
                "context_type": "text",
                "context": {},
                "root_id": huge,
            },
        ).status_code
        == 400
    )
    record = api.context("first")
    other = api.context("second")
    dream = api.begin()
    bad = api.request(
        "PUT",
        f"/v1/artifacts/bad?dream_id={dream}",
        headers={
            "If-None-Match": "*",
        },
        json={
            "content": "wrong root",
            "references": [
                {
                    "root_id": record["root_id"],
                    "record_id": other["id"],
                }
            ],
        },
    )
    assert bad.status_code == 422


def test_summary_and_global_injection_limits(custom_api):
    api, _ = custom_api(
        index_max_chars=200, buffer_max_items=100, injection_max_chars=1224, preview_max_chars=100
    )
    records = [api.context("中文资料" * 20) for _ in range(5)]
    injection = api.get("/v1/injection")
    assert len(injection.text) <= 1224
    assert injection.json()["omitted_count"] > 0
    dream = api.begin()
    too_large = api.request(
        "PUT",
        f"/v1/artifacts/_index?dream_id={dream}",
        headers={
            "If-None-Match": "*",
        },
        json={"content": "x" * 201},
    )
    assert too_large.status_code == 413
    inputs = api.get("/v1/records", params={"dream_id": dream}).json()["items"]
    assert len(inputs) == len(records)


def test_stale_etags_cannot_overwrite_recreated_or_inherited_cards(api):
    original = api.context("source")
    api.publish(original, "published")
    dream = api.begin()
    inherited = api.get(f"/v1/artifacts/note?dream_id={dream}")
    updated = api.put(dream, "note", "updated", references=reference(original))
    assert inherited.headers["etag"] != updated.headers["etag"]
    assert (
        api.request(
            "DELETE",
            f"/v1/artifacts/note?dream_id={dream}",
            headers={
                "If-Match": updated.headers["etag"],
            },
        ).status_code
        == 204
    )
    recreated = api.put(dream, "note", "recreated", references=reference(original))
    assert recreated.headers["etag"] != updated.headers["etag"]
    stale = api.request(
        "PUT",
        f"/v1/artifacts/note?dream_id={dream}",
        headers={
            "If-Match": updated.headers["etag"],
        },
        json={"content": "stale write", "references": reference(original)},
    )
    assert stale.status_code == 412
    fresh = api.put(dream, "fresh", "first creation", references=reference(original))
    assert (
        api.request(
            "DELETE",
            f"/v1/artifacts/fresh?dream_id={dream}",
            headers={
                "If-Match": fresh.headers["etag"],
            },
        ).status_code
        == 204
    )
    recreated_fresh = api.put(dream, "fresh", "second creation", references=reference(original))
    assert recreated_fresh.headers["etag"] != fresh.headers["etag"]


def test_no_history_rollback_and_root_source_identity(api):
    assert api.post("/v1/dreams/rollback", json={"view_token": "empty"}).status_code == 409
    original = api.context("manual")
    response = api.post(
        "/v1/records",
        json={
            "item_type": "context",
            "context_type": "text",
            "context": {"text": "different source"},
            "root_id": original["root_id"],
            "expected_record_id": original["id"],
            "source": {"namespace": "chrome", "external_id": "url"},
        },
    )
    assert response.status_code == 412
