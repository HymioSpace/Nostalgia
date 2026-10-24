import asyncio
import hashlib
import hmac
import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException

from ..bootstrap.config import Settings
from ..core import Core
from ..core.graph import Graph
from ..core.query import Queries
from ..dreaming import DreamScheduler
from ..protocol import (
    Artifact,
    ArtifactResult,
    AssetResult,
    DreamResult,
    Error,
    ErrorResponse,
    InjectionResult,
    Page,
    RecordCreate,
    RecordPatch,
    RecordQuery,
    RecordResult,
    Rollback,
    RollbackResult,
    Search,
    SearchResult,
)
from ..protocol.graph import GraphQuery, Relation, RetrieveQuery
from ..storage import Repository, Store, identifier, now


def create_app(settings: Settings | None = None, dreamer=None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.database)
    core = Core(store, settings)
    queries = Queries(core)
    graph = Graph(core)

    @asynccontextmanager
    async def lifespan(application):
        scheduler = DreamScheduler(settings, store, core, dreamer=dreamer)
        application.state.dream_scheduler = scheduler
        stop = asyncio.Event()
        task = asyncio.create_task(scheduler.run(stop)) if scheduler.dreamer is not None else None
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                await task

    app = FastAPI(
        title="Nostalgia",
        version="0.1.0",
        lifespan=lifespan,
        responses={
            status: {"model": ErrorResponse}
            for status in (400, 401, 403, 404, 409, 412, 413, 415, 422, 503)
        },
    )
    app.state.store = store
    app.state.core = core
    bearer = HTTPBearer(auto_error=False)

    def authenticate(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if credentials and credentials.scheme.lower() == "bearer":
            for token, user in settings.tokens.items():
                if hmac.compare_digest(credentials.credentials.encode(), token.encode()):
                    return user
        raise Error(401, "unauthorized", "Valid bearer credentials required")

    User = Annotated[str, Depends(authenticate)]

    def write_headers(
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
        if_match: Annotated[str | None, Header()] = None,
        if_none_match: Annotated[str | None, Header()] = None,
    ):
        """Declare standard write headers in OpenAPI; Core checks their semantics."""

    writes = [Depends(write_headers)]

    def error_response(exc):
        payload = {
            "error": {"code": exc.code, "message": exc.message, "retryable": exc.status == 503}
        }
        if exc.details is not None:
            payload["error"]["details"] = exc.details
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(payload, status_code=exc.status, headers=headers)

    @app.exception_handler(Error)
    async def domain_error(_request, exc):
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc):
        details = [{"location": list(e["loc"]), "message": e["msg"]} for e in exc.errors()]
        return error_response(Error(400, "invalid_request", "Invalid request", details=details))

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        return error_response(Error(exc.status_code, "http_error", str(exc.detail)))

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error(_request, _exc):
        return error_response(Error(503, "storage_unavailable", "Storage temporarily unavailable"))

    @app.middleware("http")
    async def body_limit(request, call_next):
        maximum = (
            settings.max_asset_bytes
            if request.url.path == "/v1/assets"
            else (settings.max_json_bytes)
        )
        length = request.headers.get("content-length")
        if length:
            try:
                invalid = int(length) < 0 or int(length) > maximum
            except ValueError:
                return error_response(Error(400, "invalid_length", "Invalid Content-Length"))
            if invalid:
                return error_response(Error(413, "body_too_large", "Request exceeds size limit"))
        chunks, total = [], 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > maximum:
                return error_response(Error(413, "body_too_large", "Request exceeds size limit"))
            chunks.append(chunk)
        request._body = b"".join(chunks)
        request.state.raw_body = request._body
        if request._body and request.url.path != "/v1/assets":
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                return error_response(Error(415, "json_required", "Use application/json"))
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def mutate(request, user, action):
        raw = request.state.raw_body
        try:
            body = (
                json.loads(raw)
                if raw and request.url.path != "/v1/assets"
                else {
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
            signature = {
                "body": body,
                "query": sorted(request.query_params.multi_items()),
                "if_match": request.headers.get("if-match"),
                "if_none_match": request.headers.get("if-none-match"),
                "content_type": request.headers.get("content-type"),
            }
            status, data, etag = store.mutate(
                user,
                f"{request.method}:{request.url.path}",
                request.headers.get("idempotency-key"),
                signature,
                lambda conn: action(Repository(conn)),
            )
        except (ValueError, OverflowError) as exc:
            raise Error(400, "invalid_json", "Request contains invalid JSON values") from exc
        if status == 204:
            return Response(status_code=204)
        return JSONResponse(data, status_code=status, headers={"ETag": etag} if etag else None)

    def read(action):
        with store.transaction() as conn:
            return action(Repository(conn))

    @app.get("/healthz")
    def health():
        read(lambda repo: repo.head(""))
        return {"status": "ok"}

    @app.get("/v1/capabilities")
    def capabilities(user: User):
        return {
            "protocol_version": "1",
            "graph": {"version": 1, "max_nodes": 60, "max_depth": 2},
            "search_modes": ["filter", "text"],
            "text_search": "case-insensitive substring; stable ID order",
            "max_json_bytes": settings.max_json_bytes,
            "max_asset_bytes": settings.max_asset_bytes,
            "max_page_size": settings.max_page_size,
            "index_max_chars": settings.index_max_chars,
            "buffer_max_items": settings.buffer_max_items,
            "preview_max_chars": settings.preview_max_chars,
            "injection_max_chars": settings.injection_max_chars,
            "history_limit": settings.history_limit,
            "dreaming": {
                "enabled": settings.dreaming_enabled,
                "interval_seconds": settings.dreaming_interval_seconds,
                "batch_max_records": settings.dreaming_batch_max_records,
            },
        }

    @app.post("/v1/records", status_code=201, response_model=RecordResult, dependencies=writes)
    def append_record(payload: RecordCreate, request: Request, user: User):
        return mutate(request, user, lambda repo: (201, core.append(repo, user, payload), None))

    @app.get("/v1/records", response_model=Page[RecordResult])
    def list_records(query: Annotated[RecordQuery, Query()], user: User):
        return read(lambda repo: queries.records(repo, user, query))

    @app.get("/v1/records/{record_id}", response_model=RecordResult)
    def get_record(record_id: str, user: User, dream_id: str | None = None):
        valid_record_id(record_id)
        data, tag = read(lambda repo: core.get_record(repo, user, record_id, dream_id))
        return JSONResponse(data, headers={"ETag": tag})

    @app.patch("/v1/records/{record_id}", response_model=RecordResult, dependencies=writes)
    def patch_record(
        record_id: str,
        payload: RecordPatch,
        request: Request,
        user: User,
        dream_id: str,
    ):
        valid_record_id(record_id)

        def action(repo):
            data, tag = core.patch_record(
                repo,
                user,
                record_id,
                dream_id,
                payload,
                request.headers.get("if-match"),
            )
            return 200, data, tag

        return mutate(request, user, action)

    @app.get("/v1/artifacts/{artifact_id}", response_model=ArtifactResult)
    def get_artifact(artifact_id: str, user: User, dream_id: str | None = None):
        data, tag = read(lambda repo: core.artifact(repo, user, artifact_id, dream_id))
        return JSONResponse(data, headers={"ETag": tag})

    @app.put(
        "/v1/artifacts/{artifact_id}",
        response_model=ArtifactResult,
        dependencies=writes,
        responses={201: {"model": ArtifactResult}},
    )
    def put_artifact(
        artifact_id: str,
        payload: Artifact,
        request: Request,
        user: User,
        dream_id: str,
    ):
        return mutate(
            request,
            user,
            lambda repo: core.put_artifact(
                repo,
                user,
                artifact_id,
                dream_id,
                payload,
                request.headers.get("if-match"),
                request.headers.get("if-none-match"),
            ),
        )

    @app.delete("/v1/artifacts/{artifact_id}", status_code=204, dependencies=writes)
    def delete_artifact(artifact_id: str, request: Request, user: User, dream_id: str):
        def action(repo):
            core.delete_artifact(
                repo,
                user,
                artifact_id,
                dream_id,
                request.headers.get("if-match"),
            )
            return 204, None, None

        return mutate(request, user, action)

    @app.post("/v1/search", response_model=SearchResult)
    def search(payload: Search, user: User):
        return read(lambda repo: queries.search(repo, user, payload))

    @app.post("/v1/graph/query")
    def graph_query(payload: GraphQuery, user: User):
        return read(lambda repo: graph.query(repo, user, payload))

    @app.post("/v1/retrieve")
    def retrieve(payload: RetrieveQuery, user: User):
        return read(lambda repo: graph.retrieve(repo, user, payload))

    @app.put("/v1/relations", dependencies=writes)
    def put_relation(payload: Relation, request: Request, user: User, dream_id: str):
        def action(repo):
            value, tag = graph.put(repo, user, dream_id, payload, request.headers.get("if-match"))
            return 200, value, tag

        return mutate(request, user, action)

    @app.post("/v1/dreams", status_code=201, response_model=DreamResult, dependencies=writes)
    def start_dream(request: Request, user: User):
        def action(repo):
            data, tag = core.start_dream(repo, user)
            return 201, data, tag

        return mutate(request, user, action)

    @app.get("/v1/dreams/{dream_id}", response_model=DreamResult)
    def get_dream(dream_id: str, user: User):
        data, tag = read(lambda repo: core.dream_info(repo, user, dream_id))
        return JSONResponse(data, headers={"ETag": tag})

    @app.post("/v1/dreams/{dream_id}/commit", response_model=DreamResult, dependencies=writes)
    def commit(dream_id: str, request: Request, user: User):
        def action(repo):
            data, tag = core.commit(repo, user, dream_id, request.headers.get("if-match"))
            return 200, data, tag

        return mutate(request, user, action)

    @app.post("/v1/dreams/{dream_id}/abort", response_model=DreamResult, dependencies=writes)
    def abort(dream_id: str, request: Request, user: User):
        def action(repo):
            data, tag = core.abort(repo, user, dream_id)
            return 200, data, tag

        return mutate(request, user, action)

    @app.post("/v1/dreams/rollback", response_model=RollbackResult, dependencies=writes)
    def rollback(payload: Rollback, request: Request, user: User):
        return mutate(
            request,
            user,
            lambda repo: (
                200,
                core.rollback(repo, user, payload.view_token),
                None,
            ),
        )

    @app.get("/v1/assembly")
    def assembly(user: User):
        return read(lambda repo: core.assembly(repo, user))

    @app.get("/v1/events")
    def events(
        user: User,
        after: int = Query(default=0, ge=0, le=2**63 - 1),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        return read(lambda repo: core.events(repo, user, after, limit))

    @app.get("/v1/injection", response_model=InjectionResult)
    def injection(user: User):
        return read(lambda repo: core.injection(repo, user))

    @app.post("/v1/assets", status_code=201, response_model=AssetResult, dependencies=writes)
    def upload_asset(request: Request, user: User):
        def action(repo):
            asset_id = identifier()
            content_type = request.headers.get("content-type", "application/octet-stream")
            if len(content_type) > 256:
                raise Error(400, "invalid_content_type", "Content-Type is too long")
            repo.insert(
                "assets",
                id=asset_id,
                user_id=user,
                content_type=content_type,
                data=request.state.raw_body,
                created_at=now(),
            )
            return (
                201,
                {"id": asset_id, "content_type": content_type, "size": len(request.state.raw_body)},
                None,
            )

        return mutate(request, user, action)

    @app.get("/v1/assets/{asset_id}")
    def get_asset(asset_id: str, user: User):
        data = read(lambda repo: repo.one("assets", id=asset_id, user_id=user))
        if not data:
            raise Error(404, "not_found", "Asset not found")
        return Response(
            data["data"],
            media_type=data["content_type"],
            headers={
                "Content-Disposition": 'attachment; filename="asset"',
            },
        )

    return app


def valid_record_id(value):
    if len(value) > 19 or not value.isascii() or not value.isdigit() or not 0 < int(value) < 2**63:
        raise Error(400, "invalid_id", "Invalid record ID")
