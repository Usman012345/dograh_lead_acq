"""FastAPI read-only application for the dograh_caller pipeline dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

import config
from dashboard.data import AUDIO_TRACKS, TrackerDataStore
from dashboard.media import DograhMedia, MediaUnavailable, parse_transcript
from dashboard.runner import BatchRunner, RunnerBusyError

DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "static"

_VALID_VIEWS = frozenset({
    "incoming",
    "number_verifier",
    "decision_maker",
    "non_qualified",
    "qualified",
    "calling_agent",
    "call_results",
    "transferred",
})

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; media-src 'self' https:; connect-src 'self'; font-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


def _host_name(header: str) -> str:
    try:
        return (urlsplit(f"//{header}").hostname or "").lower()
    except ValueError:
        return ""


def _default_media() -> Optional[Any]:
    if not config.DOGRAH_API_KEY:
        return None
    return DograhMedia(config.DOGRAH_BASE_URL, config.DOGRAH_API_KEY,
                       verify=config.VERIFY_SSL, timeout=config.REQUEST_TIMEOUT)


class RunnerStartRequest(BaseModel):
    batch_size: int = Field(default=300, ge=1, le=1000)
    num_batches: int = Field(default=1, ge=1, le=100)
    # Call again (in a later batch) leads whose call reached nobody; None = RETRY_FAILED_CALLS
    retry_failed_calls: Optional[bool] = None


def create_app(
    tracker_path: Optional[Path] = None,
    *,
    static_dir: Optional[Path] = None,
    services_file: Optional[Path] = None,
    threshold: Optional[float] = None,
    refresh_seconds: Optional[int] = None,
    start_watcher: bool = True,
    media: Optional[Any] = None,
    allowed_hosts: Optional[Iterable[str]] = None,
    demo: bool = False,
    runner: Optional[BatchRunner] = None,
) -> FastAPI:
    resolved_tracker = Path(tracker_path or config.PIPELINE_TRACKER_FILE)
    resolved_static = Path(static_dir or DEFAULT_STATIC_DIR).resolve()
    media_source = media if media is not None else _default_media()
    hosts = frozenset(h.lower() for h in allowed_hosts) if allowed_hosts else None

    if runner is not None and getattr(runner, "_store", None) is not None:
        store = runner._store
        batch_runner = runner
    else:
        store = TrackerDataStore(
            resolved_tracker,
            services_file=services_file,
            threshold=threshold,
            refresh_seconds=refresh_seconds,
            start_watcher=start_watcher,
            demo=demo,
        )
        batch_runner = BatchRunner(store=store, demo=demo)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            batch_runner.close()
            store.close()

    app = FastAPI(title="dograh_caller Dashboard", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.runner = batch_runner

    @app.middleware("http")
    async def protect_responses(request: Request, call_next):
        # With no login, a local dashboard must refuse pages that point their own
        # domain name at this computer (DNS rebinding) to read lead data.
        if hosts is not None and _host_name(request.headers.get("host", "")) not in hosts:
            return PlainTextResponse("This address is not allowed.", status_code=400)
        response: Response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers[name] = value
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def _active_runner_batch() -> Optional[dict[str, Any]]:
        status = batch_runner.get_status()
        if status.get("active") and status.get("batch_id"):
            return status
        return None

    @app.get("/api/health")
    def api_health() -> JSONResponse:
        return JSONResponse(store.get_health())

    @app.get("/api/batches")
    def api_batches() -> JSONResponse:
        active = _active_runner_batch()
        batches = store.list_batches(active_batch=active)
        return JSONResponse({
            "batches": batches,
            "active_batch_id": active["batch_id"] if active else None,
            "latest_batch_id": batches[0]["batch_id"] if batches else None,
        })

    @app.get("/api/summary")
    def api_summary(batch_id: Optional[str] = Query(None)) -> JSONResponse:
        active = _active_runner_batch()
        summary = store.get_summary(batch_id=batch_id, active_batch=active)
        summary["runner"] = batch_runner.get_status()
        return JSONResponse(summary)

    @app.get("/api/runner")
    def api_runner_status() -> JSONResponse:
        return JSONResponse(batch_runner.get_status())

    @app.post("/api/runner/start")
    def api_runner_start(payload: Optional[RunnerStartRequest] = None) -> JSONResponse:
        req = payload or RunnerStartRequest()
        try:
            if req.retry_failed_calls is None:
                status = batch_runner.start(batch_size=req.batch_size, num_batches=req.num_batches)
            else:
                status = batch_runner.start(batch_size=req.batch_size, num_batches=req.num_batches,
                                            retry_failed_calls=req.retry_failed_calls)
        except RunnerBusyError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return JSONResponse(status)

    @app.post("/api/runner/stop")
    def api_runner_stop() -> JSONResponse:
        return JSONResponse(batch_runner.stop())

    @app.get("/api/leads")
    def api_leads(
        view: str = Query(...),
        service: Optional[str] = Query(None),
        filter: Optional[str] = Query(None),
        q: Optional[str] = Query(None),
        sort: str = Query("lead_id"),
        order: str = Query("asc"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        batch_id: Optional[str] = Query(None),
    ) -> JSONResponse:
        if view not in _VALID_VIEWS:
            raise HTTPException(status_code=400, detail=f"Unknown view: {view}")
        active = _active_runner_batch()
        data = store.get_leads(
            view=view,
            service=service,
            filter_val=filter,
            q=q,
            sort=sort,
            order=order,
            page=page,
            page_size=page_size,
            batch_id=batch_id,
            active_batch_id=str(active["batch_id"]) if active else None,
        )
        return JSONResponse(data)

    @app.get("/api/leads/{lead_id}")
    def api_lead_detail(lead_id: str) -> JSONResponse:
        detail = store.get_lead_detail(lead_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        return JSONResponse(detail)

    @app.get("/api/leads/{lead_id}/transcript")
    def api_lead_transcript(lead_id: str) -> JSONResponse:
        key = store.media_key(lead_id, "transcript")
        if key is None:
            raise HTTPException(status_code=404, detail="No transcript for this lead")
        if media_source is None:
            raise HTTPException(status_code=503, detail="Transcripts are not available")
        try:
            return JSONResponse({"turns": parse_transcript(media_source.fetch_text(key))})
        except MediaUnavailable as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    @app.get("/api/leads/{lead_id}/audio/{track}")
    def api_lead_audio(lead_id: str, track: str, request: Request) -> Response:
        if track not in AUDIO_TRACKS:
            raise HTTPException(status_code=404, detail="Unknown recording")
        key = store.media_key(lead_id, track)
        if key is None:
            raise HTTPException(status_code=404, detail="No recording for this lead")
        if media_source is None:
            raise HTTPException(status_code=503, detail="Recordings are not available")
        try:
            audio = media_source.open_audio(key, request.headers.get("range"))
        except MediaUnavailable as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

        def body():
            try:
                yield from audio.chunks
            finally:
                audio.close()

        return StreamingResponse(body(), status_code=audio.status_code, headers=audio.headers)

    @app.get("/")
    def serve_index() -> Response:
        index_path = resolved_static / "index.html"
        if index_path.is_file():
            return FileResponse(index_path)
        return JSONResponse(
            {"status": "ok", "message": "Dashboard static assets not built yet."},
            status_code=200,
        )

    @app.get("/{asset_path:path}")
    def serve_static(asset_path: str) -> Response:
        if asset_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        if not resolved_static.is_dir():
            raise HTTPException(status_code=404, detail="Not found")
        candidate = (resolved_static / asset_path).resolve()
        try:
            if not candidate.is_relative_to(resolved_static):
                raise HTTPException(status_code=404, detail="Not found")
        except ValueError:
            raise HTTPException(status_code=404, detail="Not found") from None
        if candidate.is_file():
            return FileResponse(candidate)
        index_path = resolved_static / "index.html"
        if "." not in Path(asset_path).name and index_path.is_file():
            return FileResponse(index_path)
        raise HTTPException(status_code=404, detail="Not found")

    return app
