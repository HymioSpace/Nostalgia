def relation(api, dream, a, b, kind="related"):
    tag = api.get(f"/v1/dreams/{dream}").headers["etag"]
    return api.request(
        "PUT",
        f"/v1/relations?dream_id={dream}",
        headers={"If-Match": tag},
        json={
            "source": {"kind": "record", "id": a["id"]},
            "target": {"kind": "record", "id": b["id"]},
            "type": kind,
            "rationale": "Both notes explicitly describe the same routine",
            "evidence_refs": [{"root_id": a["root_id"], "record_id": a["id"]}],
        },
    )


def test_arbitrary_relations_atomic_publish_and_snapshot(api):
    a, b = api.context("walk practice"), api.context("walk listening")
    dream = api.begin()
    api.mark(dream, a)
    api.mark(dream, b)
    api.index(dream)
    assert relation(api, dream, a, b).status_code == 200
    before = api.post("/v1/graph/query", json={}).json()
    assert before["edges"] == []
    assert api.commit(dream).status_code == 200
    page = api.post("/v1/graph/query", json={"seed": {"kind": "record", "id": b["id"]}}).json()
    assert len(page["nodes"]) == 2
    assert page["edges"][0]["type"] == "related"
    assert api.post("/v1/graph/query", json={"snapshot": before["snapshot"]}).status_code == 409
    result = api.post("/v1/retrieve", json={"query": "listening"}).json()
    assert len(result["nodes"]) == 2


def test_relation_invalid_endpoint_blocks_entire_commit(api):
    a, b = api.context("one"), {"id": "999999"}
    dream = api.begin()
    api.mark(dream, a)
    api.index(dream)
    assert relation(api, dream, a, b).status_code == 200
    assert api.commit(dream).status_code == 422
    assert api.post("/v1/graph/query", json={}).json()["edges"] == []


def test_supersedes_cycle_rejected(api):
    a, b = api.context("one"), api.context("two")
    dream = api.begin()
    api.mark(dream, a)
    api.mark(dream, b)
    api.index(dream)
    assert relation(api, dream, a, b, "supersedes").status_code == 200
    assert relation(api, dream, b, a, "supersedes").status_code == 200
    assert api.commit(dream).status_code == 422
