"""Periodic dreaming owned by Nostalgia and backed by a configured proposal provider."""

import asyncio
import json
import logging
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime

from .core import Core
from .protocol import Artifact, Error, RecordPatch, encode
from .storage import Repository, Store

_log = logging.getLogger(__name__)


class HTTPDreamer:
    """Send a fixed context window to an operator-configured proposal endpoint."""

    def __init__(self, url, token=None, timeout=120):
        self.url = url
        self.token = token
        self.timeout = timeout

    def propose(self, task):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(
            self.url,
            data=json.dumps(task, ensure_ascii=False, allow_nan=False).encode(),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise RuntimeError("Dream provider rejected the task")
                raw = response.read(1_048_577)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError("Dream provider unavailable") from exc
        if len(raw) > 1_048_576:
            raise RuntimeError("Dream provider response is too large")
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise RuntimeError("Dream provider returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Dream provider must return an object")
        return value


class DreamScheduler:
    """Run one bounded Nostalgia dream cycle for every known context user."""

    def __init__(self, settings, store: Store, core: Core, dreamer=None):
        self.settings = settings
        self.store = store
        self.core = core
        self.dreamer = dreamer or self._configured_dreamer()
        self._lock = threading.Lock()

    async def run(self, stop):
        if self.dreamer is None:
            return
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self.settings.dreaming_interval_seconds
                )
            except TimeoutError:
                try:
                    await asyncio.to_thread(self.run_once)
                except Exception:
                    _log.exception("Scheduled dreaming cycle failed")

    def run_once(self):
        if self.dreamer is None:
            return []
        if not self._lock.acquire(blocking=False):
            return []
        try:
            return [
                result
                for user in self.settings.dreaming_users
                if (result := self._dream_user(user)) is not None
            ]
        finally:
            self._lock.release()

    def _configured_dreamer(self):
        if not self.settings.dreaming_enabled:
            return None
        url = self.settings.dreaming_provider_url
        if not url:
            raise ValueError("dreaming_provider_url is required when dreaming is enabled")
        token = None
        if self.settings.dreaming_provider_token_file:
            token = self.settings.dreaming_provider_token_file.read_text().strip()
        return HTTPDreamer(url, token)

    def _dream_user(self, user):
        with self.store.transaction() as conn:
            repo = Repository(conn)
            active = self.core.active(repo, user)
            cursor = active["cursor"] if active else 0
            draft = repo.one("versions", user_id=user, state="draft")
            if draft:
                if self._draft_age(draft) <= self.settings.dreaming_draft_timeout_seconds:
                    return None
                info, _ = self.core.abort(repo, user, draft["id"])
                self._event(repo, user, "dreaming.stale_aborted", {"dream_id": draft["id"]})
                cursor = self.core.active(repo, user)["cursor"]
            candidates = repo.records(user, repo.head(user), after=cursor)
            selected = self._window(candidates)
            if not selected:
                return None
            cutoff = selected[-1]["id"]
            info, _ = self.core.start_dream(repo, user, cutoff=cutoff)

        dream_id = info["dream_id"]
        try:
            task = self._task(user, dream_id)
            proposal = self.dreamer.propose(task)
            result = self._apply(user, dream_id, proposal)
            return {"user_id": user, "dream_id": dream_id, "state": result["state"]}
        except Exception as exc:
            with self.store.transaction(write=True) as conn:
                repo = Repository(conn)
                try:
                    self.core.abort(repo, user, dream_id)
                except Error:
                    pass
                self._event(
                    repo,
                    user,
                    "dreaming.failed",
                    {"dream_id": dream_id, "error": type(exc).__name__},
                )
            _log.warning(
                "Dreaming failed for one user: %s: %s details=%s",
                type(exc).__name__,
                exc,
                getattr(exc, "details", None),
            )
            return {"user_id": user, "dream_id": dream_id, "state": "failed"}

    def _window(self, records):
        selected = []
        size = 0
        for row in records:
            row_size = len(encode(json.loads(row["data"])))
            if selected and (
                len(selected) >= self.settings.dreaming_batch_max_records
                or size + row_size > self.settings.dreaming_batch_max_chars
            ):
                break
            if row_size > self.settings.dreaming_batch_max_chars:
                break
            selected.append(row)
            size += row_size
        return selected

    def _task(self, user, dream_id):
        with self.store.transaction() as conn:
            repo = Repository(conn)
            info, etag = self.core.dream_info(repo, user, dream_id)
            rows = repo.records(
                user,
                int(info["through_id"]),
                after=int(info["after_id"]),
            )
            records = [
                {
                    "id": str(row["id"]),
                    "root_id": str(row["root_id"]),
                    "item_type": row["item_type"],
                    "created_at": row["created_at"],
                    **json.loads(row["data"]),
                }
                for row in rows
            ]
            active = self.core.active(repo, user)
            artifacts = []
            for detail in repo.find("details", version_id=active["id"], kind="artifact"):
                artifacts.append({"id": detail["item_id"], **json.loads(detail["data"])})
            return {
                "protocol_version": "1",
                "task": "dream",
                "user_id": user,
                "dream_id": dream_id,
                "etag": etag,
                "after_id": info["after_id"],
                "through_id": info["through_id"],
                "records": records,
                "artifacts": artifacts,
            }

    def _apply(self, user, dream_id, proposal):
        if not isinstance(proposal, dict):
            raise RuntimeError("Dream proposal must be an object")
        artifacts = proposal.get("artifacts")
        record_states = proposal.get("record_states")
        if not isinstance(artifacts, list) or not isinstance(record_states, list):
            raise RuntimeError("Dream proposal requires artifacts and record_states arrays")
        with self.store.transaction(write=True) as conn:
            repo = Repository(conn)
            for item in record_states:
                if not isinstance(item, dict) or "record_id" not in item:
                    raise RuntimeError("Dream record state requires record_id")
                record_id = str(item["record_id"])
                _, etag = self.core.get_record(repo, user, record_id, dream_id)
                patch = RecordPatch.model_validate(
                    {key: value for key, value in item.items() if key != "record_id"}
                )
                self.core.patch_record(repo, user, record_id, dream_id, patch, etag)
            ordered = sorted(
                artifacts,
                key=lambda item: isinstance(item, dict)
                and item.get("id") == "_index",
            )
            for item in ordered:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise RuntimeError("Dream artifact requires a string id")
                item_id = item["id"]
                payload = Artifact.model_validate(
                    {key: value for key, value in item.items() if key != "id"}
                )
                try:
                    _, etag = self.core.artifact(repo, user, item_id, dream_id)
                    match, create = etag, None
                except Error as exc:
                    if exc.code != "not_found":
                        raise
                    match, create = None, "*"
                self.core.put_artifact(
                    repo, user, item_id, dream_id, payload, match, create
                )
            _, etag = self.core.dream_info(repo, user, dream_id)
            result, _ = self.core.commit(repo, user, dream_id, etag)
            return result

    @staticmethod
    def _draft_age(row):
        try:
            created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return 0
        return max(0, (datetime.now(UTC) - created).total_seconds())

    @staticmethod
    def _event(repo, user, kind, data):
        from .storage import now

        repo.insert(
            "context_events",
            user_id=user,
            kind=kind,
            data=encode(data),
            created_at=now(),
        )
