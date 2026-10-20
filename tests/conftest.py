import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from nostalgia.api import create_app
from nostalgia.bootstrap.config import Settings

TOKENS = {"alice": "alice-test-token-123456", "bob": "bob-test-token-12345678"}


class Client:
    def __init__(self, client, user="alice"):
        self.client = client
        self.user = user

    def request(self, method, path, *, key=None, headers=None, **kwargs):
        extra = {"Authorization": f"Bearer {TOKENS[self.user]}", **(headers or {})}
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and path != "/v1/search":
            extra["Idempotency-Key"] = key or uuid.uuid4().hex
        return self.client.request(method, path, headers=extra, **kwargs)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def context(self, text, **extra):
        response = self.post(
            "/v1/records",
            json={
                "item_type": "context",
                "context_type": "text",
                "context": {"text": text},
                **extra,
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    def command(self, content, command_type="request", roots=None):
        response = self.post(
            "/v1/records",
            json={
                "item_type": "command",
                "command": {
                    "command_type": command_type,
                    "command_content": content,
                    "target_root_ids": roots or [],
                },
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    def begin(self):
        response = self.post("/v1/dreams")
        assert response.status_code == 201, response.text
        return response.json()["dream_id"]

    def mark(self, dream, record, status="active", processing="applied", reason=None):
        record_id = record["id"] if isinstance(record, dict) else record
        path = f"/v1/records/{record_id}?dream_id={dream}"
        current = self.get(path)
        assert current.status_code == 200, current.text
        payload = {"processing_state": processing}
        if current.json()["item_type"] == "context":
            payload["status"] = status
        if reason:
            payload["reason"] = reason
        response = self.request(
            "PATCH",
            path,
            json=payload,
            headers={
                "If-Match": current.headers["etag"],
            },
        )
        assert response.status_code == 200, response.text
        return response

    def put(self, dream, card_id, content, references=None, links=None, **extra):
        path = f"/v1/artifacts/{card_id}?dream_id={dream}"
        current = self.get(path)
        header = (
            {"If-Match": current.headers["etag"]}
            if current.status_code == 200
            else {
                "If-None-Match": "*",
            }
        )
        response = self.request(
            "PUT",
            path,
            headers=header,
            json={
                "content": content,
                "references": references or [],
                "links": links or [],
                **extra,
            },
        )
        assert response.status_code in {200, 201}, response.text
        return response

    def index(self, dream, content="summary", links=None):
        return self.put(dream, "_index", content, links=links)

    def commit(self, dream, **kwargs):
        current = self.get(f"/v1/dreams/{dream}")
        return self.post(
            f"/v1/dreams/{dream}/commit",
            headers={
                "If-Match": current.headers["etag"],
            },
            **kwargs,
        )

    def publish(self, record, content="summary", card_id="note"):
        dream = self.begin()
        self.mark(dream, record)
        self.put(
            dream,
            card_id,
            content,
            references=[
                {
                    "root_id": record["root_id"],
                    "record_id": record["id"],
                }
            ],
            topics=["project"],
        )
        self.index(dream, content, links=[card_id])
        response = self.commit(dream)
        assert response.status_code == 200, response.text
        return dream


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database=tmp_path / "nostalgia.sqlite3", tokens={v: k for k, v in TOKENS.items()}
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def api(app):
    with TestClient(app) as raw:
        yield Client(raw)


@pytest.fixture
def custom_api(settings):
    clients = []

    def factory(**changes):
        config = replace(settings, **changes)
        raw = TestClient(create_app(config))
        raw.__enter__()
        clients.append(raw)
        return Client(raw), config

    yield factory
    for raw in clients:
        raw.__exit__(None, None, None)
