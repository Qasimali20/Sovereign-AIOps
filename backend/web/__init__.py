"""HTTP layer: app factory, security middleware, API routers, and the SPA."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from backend.core import analytics, config, store
from backend.monitoring import metrics
from backend.incidents.runtime import runtime
from backend.web.routes import agentops, auth, gateway, governance, incidents, infra, llmops, soc, users
from backend.web.security import guard

DIST = config.ROOT / "frontend" / "dist"


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime.start()
        yield

    app = FastAPI(title="Sovereign AI Ops", lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.middleware("http")(guard)

    api = APIRouter(prefix="/api")
    api.add_api_route("/health", lambda: {"ok": True, "workflows_running": runtime.running}, methods=["GET"])
    for r in (auth.router, incidents.router, infra.router, governance.router, users.router, gateway.router, soc.router,
              agentops.router, llmops.router):
        api.include_router(r)
    app.include_router(api)
    _mount_metrics(app)
    _mount_spa(app)
    return app


# Scrape endpoints carry no session auth (Prometheus has none), so only the fleet itself may read them.
SCRAPERS = {"127.0.0.1", "::1"} | {n.lan_ip for n in config.NODES} | {n.ts_ip for n in config.NODES}


def _mount_metrics(app: FastAPI) -> None:
    def scraper(request: Request) -> None:
        if (request.client.host if request.client else None) not in SCRAPERS:
            raise HTTPException(403, "metrics are readable from fleet nodes only")

    @app.get("/metrics", include_in_schema=False)
    def prometheus(request: Request):
        scraper(request)
        det = runtime.scan_info | {"workflows_running": len(runtime.running), "audit_ok": runtime.audit_check.get("ok"),
                                   "audit_events": runtime.audit_check.get("events")}
        body = metrics.render(metrics.fleet_raw(), dict(runtime.services), analytics.pipeline_counts(), det)
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/metrics/targets", include_in_schema=False)
    def targets(request: Request):
        scraper(request)
        return JSONResponse(metrics.vllm_targets(metrics.fleet_raw()))

    @app.get("/metrics/relay/{node}/{port}", include_in_schema=False)
    def relay(node: str, port: int, request: Request):
        scraper(request)
        body = metrics.relay(node, port, metrics.fleet_raw())
        if body is None:
            raise HTTPException(404, "not a model server the fleet monitors report")
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


def _mount_spa(app: FastAPI) -> None:
    """Hashed assets are immutable (cache forever); index.html is always revalidated."""
    if (DIST / "assets").is_dir():
        class Immutable(StaticFiles):
            async def get_response(self, path, scope):
                resp = await super().get_response(path, scope)
                if resp.status_code == 200:
                    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
                return resp
        app.mount("/assets", Immutable(directory=DIST / "assets"), name="assets")

    root = DIST.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        f = (DIST / path).resolve()
        if path and f.is_file() and root in f.parents:
            return FileResponse(f)
        index = DIST / "index.html"
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"detail": "frontend not built: cd frontend && npm run build"}, status_code=503)


app = create_app()
