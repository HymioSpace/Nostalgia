"""Run an isolated HTTP scenario, including two abrupt process/container restarts."""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


class Runtime:
    def __init__(self, docker, image, directory):
        self.docker = docker
        self.image = image
        self.name = f"nostalgia-smoke-{uuid.uuid4().hex[:10]}"
        self.volume = f"{self.name}-data"
        self.directory = directory
        self.token = secrets.token_urlsafe(32)
        self.process = None
        self.created = False
        self.url = None
        self.env = {**os.environ, "NOSTALGIA_TOKENS_JSON": json.dumps({self.token: "smoke"})}
        self.log = (directory / "service.log").open("w+")

    @staticmethod
    def docker_command(*arguments):
        return subprocess.check_output(["docker", *arguments], text=True, stderr=subprocess.STDOUT)

    def start(self):
        if self.docker:
            self.docker_command("volume", "create", self.volume)
            self.created = True
            subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    self.name,
                    "-p",
                    "127.0.0.1::8000",
                    "-v",
                    f"{self.volume}:/data",
                    "-e",
                    "NOSTALGIA_TOKENS_JSON",
                    self.image,
                ],
                env=self.env,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            port = self.docker_command("port", self.name, "8000/tcp").strip().rsplit(":", 1)[1]
            self.url = f"http://127.0.0.1:{port}"
        else:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            self.url = f"http://127.0.0.1:{port}"
            self.env.update(
                {
                    "NOSTALGIA_HOST": "127.0.0.1",
                    "NOSTALGIA_PORT": str(port),
                    "NOSTALGIA_DATABASE": str(self.directory / "nostalgia.sqlite3"),
                }
            )
            self.spawn()
        self.wait_ready()

    def spawn(self):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "nostalgia"],
            cwd=ROOT,
            env=self.env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )

    def wait_ready(self):
        for _ in range(100):
            try:
                response = httpx.get(f"{self.url}/healthz", timeout=1)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError("Service did not become healthy")

    def restart_abruptly(self):
        if self.docker:
            self.docker_command("kill", "--signal", "KILL", self.name)
            self.docker_command("start", self.name)
            port = self.docker_command("port", self.name, "8000/tcp").strip().rsplit(":", 1)[1]
            self.url = f"http://127.0.0.1:{port}"
        else:
            self.process.kill()
            self.process.wait(timeout=10)
            self.spawn()
        self.wait_ready()

    def close(self):
        if self.docker:
            if self.created:
                subprocess.run(
                    ["docker", "rm", "-f", self.name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            subprocess.run(
                ["docker", "volume", "rm", self.volume],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        elif self.process:
            self.process.terminate()
            self.process.wait(timeout=10)
        self.log.close()


def run(runtime):
    def call(method, path, status=200, **kwargs):
        headers = {"Authorization": f"Bearer {runtime.token}"}
        headers.update(kwargs.pop("headers", {}))
        if method not in {"GET", "HEAD"} and path != "/v1/search":
            headers.setdefault("Idempotency-Key", uuid.uuid4().hex)
        response = httpx.request(method, runtime.url + path, headers=headers, timeout=10, **kwargs)
        assert response.status_code == status, (
            f"{method} {path}: {response.status_code} {response.text}"
        )
        return response

    def mark(dream, record, status="active"):
        path = f"/v1/records/{record['id']}?dream_id={dream}"
        current = call("GET", path)
        value = {"processing_state": "applied"}
        if record["item_type"] == "context":
            value["status"] = status
        call("PATCH", path, json=value, headers={"If-Match": current.headers["etag"]})

    def put(dream, name, content, references=None, links=None):
        path = f"/v1/artifacts/{name}?dream_id={dream}"
        current = httpx.get(
            runtime.url + path, headers={"Authorization": f"Bearer {runtime.token}"}
        )
        headers = (
            {"If-Match": current.headers["etag"]}
            if current.status_code == 200
            else {
                "If-None-Match": "*",
            }
        )
        call(
            "PUT",
            path,
            status=200 if current.status_code == 200 else 201,
            headers=headers,
            json={"content": content, "references": references or [], "links": links or []},
        )

    def commit(dream):
        etag = call("GET", f"/v1/dreams/{dream}").headers["etag"]
        key = uuid.uuid4().hex
        result = call(
            "POST",
            f"/v1/dreams/{dream}/commit",
            headers={
                "If-Match": etag,
                "Idempotency-Key": key,
            },
        )
        assert (
            result.json()
            == call(
                "POST",
                f"/v1/dreams/{dream}/commit",
                headers={
                    "If-Match": etag,
                    "Idempotency-Key": key,
                },
            ).json()
        )

    def context(text):
        return call(
            "POST",
            "/v1/records",
            status=201,
            json={
                "item_type": "context",
                "context_type": "text",
                "context": {"text": text},
            },
        ).json()

    record = context("Hymio is a personal agent")
    asset = call(
        "POST",
        "/v1/assets",
        status=201,
        content=b"original bytes",
        headers={
            "Content-Type": "application/octet-stream",
        },
    ).json()
    dream = call("POST", "/v1/dreams", status=201).json()["dream_id"]
    mark(dream, record)
    references = [{"root_id": record["root_id"], "record_id": record["id"]}]
    put(dream, "purpose", "personal use", references)
    put(dream, "_index", "Hymio: personal use", links=["purpose"])
    later = context("arrived during dreaming")
    assert call("GET", "/v1/injection").json()["summary"]["content"] == ""
    print("HTTP: ingestion, assets and isolated draft passed", flush=True)

    runtime.restart_abruptly()
    assert call("GET", f"/v1/dreams/{dream}").json()["state"] == "draft"
    assert call("GET", f"/v1/assets/{asset['id']}").content == b"original bytes"
    commit(dream)
    data = call("GET", "/v1/injection").json()
    assert data["summary"]["content"] == "Hymio: personal use"
    assert [i["id"] for i in data["buffer"]] == [later["id"]]
    print("Restart 1: draft recovery, commit retry and buffer boundary passed", flush=True)

    runtime.restart_abruptly()
    assert call("GET", "/v1/artifacts/purpose").json()["content"] == "personal use"
    assert call("GET", f"/v1/records/{record['id']}").json()["status"] == "active"
    second = call("POST", "/v1/dreams", status=201).json()["dream_id"]
    mark(second, later)
    put(second, "purpose", "updated personal use", references)
    put(second, "_index", "updated index", links=["purpose"])
    commit(second)
    view = call("GET", "/v1/injection").json()
    call("POST", "/v1/dreams/rollback", json={"view_token": view["view_token"]})
    assert call("GET", "/v1/injection").json()["summary"]["content"] == "Hymio: personal use"
    assert call("GET", f"/v1/records/{later['id']}").json()["processing_state"] == "pending"
    print("Restart 2: publication persistence and rollback passed", flush=True)

    command = call(
        "POST",
        "/v1/records",
        status=201,
        json={
            "item_type": "command",
            "command": {
                "command_type": "delete",
                "command_content": "forget the original",
                "target_root_ids": [record["root_id"]],
            },
        },
    ).json()
    third = call("POST", "/v1/dreams", status=201).json()["dream_id"]
    mark(third, later)
    mark(third, command)
    mark(third, record, status="deleted")
    linked = call(
        "POST",
        "/v1/search",
        json={"target": "artifacts", "dream_id": third, "filters": {"root_id": record["root_id"]}},
    ).json()
    assert [i["id"] for i in linked["items"]] == ["purpose"]
    card = call("GET", f"/v1/artifacts/purpose?dream_id={third}")
    call(
        "DELETE",
        f"/v1/artifacts/purpose?dream_id={third}",
        status=204,
        headers={"If-Match": card.headers["etag"]},
    )
    put(third, "_index", "original removed")
    commit(third)
    assert call("GET", f"/v1/records/{record['id']}").json()["status"] == "deleted"
    call("GET", "/v1/artifacts/purpose", status=404)
    print("HTTP: deferred deletion and reverse provenance passed", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--docker", action="store_true", help="Run the built image instead of Python"
    )
    parser.add_argument("--image", default="nostalgia:mvp")
    options = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="nostalgia-smoke-") as directory:
        runtime = Runtime(options.docker, options.image, Path(directory))
        try:
            runtime.start()
            run(runtime)
            print("All real HTTP smoke scenarios passed.", flush=True)
        except BaseException:
            if runtime.docker and runtime.created:
                print(runtime.docker_command("logs", "--tail", "60", runtime.name), file=sys.stderr)
            else:
                runtime.log.flush()
                print((Path(directory) / "service.log").read_text(), file=sys.stderr)
            raise
        finally:
            runtime.close()


if __name__ == "__main__":
    main()
