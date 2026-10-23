from conftest import Client


def test_source_filter_and_stable_assembly(api):
    record = api.context(
        "偏好茶", source={"kind": "user", "namespace": "saved", "external_id": "1"}
    )
    dream = api.begin()
    api.mark(dream, record)
    api.put(
        dream,
        "_core",
        "偏好茶",
        references=[{"root_id": record["root_id"], "record_id": record["id"]}],
    )
    api.index(dream)
    assert api.commit(dream).status_code == 200
    stable = api.get("/v1/assembly").json()["stable"]
    next_record = api.context(
        "明天开会", source={"kind": "connector", "namespace": "calendar", "external_id": "1"}
    )
    api.publish(next_record)
    assert api.get("/v1/assembly").json()["stable"] == stable
    result = api.post("/v1/search", json={"target": "records", "filters": {"source_kind": "user"}})
    assert [r["id"] for r in result.json()["items"]] == [record["id"]]


def test_events_transaction_replay_and_isolation(api):
    payload = {"item_type": "context", "context_type": "text", "context": {"text": "hello"}}
    for _ in range(2):
        assert api.post("/v1/records", key="same", json=payload).status_code == 201
    first = api.get("/v1/events?limit=1").json()
    assert len(first["items"]) == 1
    assert first == api.get("/v1/events?limit=1").json()
    assert Client(api.client, "bob").get("/v1/events").json()["items"] == []
    assert api.get("/v1/events?after=" + first["next_after"]).json()["items"] == []
    assert (
        api.post(
            "/v1/records", key="same", json={**payload, "context": {"text": "changed"}}
        ).status_code
        == 409
    )
    assert len(api.get("/v1/events").json()["items"]) == 1
