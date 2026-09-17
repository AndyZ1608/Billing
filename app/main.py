from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.internal import router as internal_router
from app.api.invoicing import router as invoicing_router
from app.api.metering import router as metering_router
from app.api.pricing import router as pricing_router
from app.api.rating import router as rating_router
from app.api.routes import router
from app.billing.policy import load_policy
from app.core.config import Settings
from app.core.logging import configure_logging, event
from app.db.session import make_engine, make_sessions
from app.metering.engine import MeteringEngine
from app.notifications.consumer import NovaConsumer
from app.rating.engine import RatingEngine
from app.sync.engine import SyncManager, ensure_cloud

STATIC = Path(__file__).parent / "static"


def create_app(settings=None, engine=None, client_factory=None):
    settings = settings or Settings()
    engine = engine or make_engine(settings.database_url.get_secret_value())
    if settings.app_env == "production" and engine.dialect.name != "postgresql":
        raise ValueError("Production requires PostgreSQL")
    sessions = make_sessions(engine)
    manager = SyncManager(engine, sessions, settings, client_factory)
    metering = MeteringEngine(engine, sessions, settings, manager.gate)
    rating = RatingEngine(engine, sessions, settings, manager.gate)
    notifications = NovaConsumer(engine, sessions, settings, manager, metering)
    metering.on_completed = rating.after_metering
    if settings.metering_enabled:
        manager.on_completed = metering.after_sync

    @asynccontextmanager
    async def lifespan(application):
        configure_logging()
        # Schema must already exist; only Alembic creates or updates it.
        with sessions.begin() as db:
            ensure_cloud(db, settings)
            metering.register_policy(db)
        if settings.sync_enabled:
            manager.start()
        notifications.start()
        yield
        notifications.close()
        manager.close()
        engine.dispose()

    application = FastAPI(
        title="OpenStack Billing Inventory",
        version="0.4.0",
        lifespan=lifespan,
        description=(
            "Current inventory, historical usage and auditable pre-tax rated charges. "
            "Billing cycles, immutable invoices and adjustments; no payment processing."
        ),
    )
    application.state.notifications = notifications
    application.state.settings = settings
    application.state.sessions = sessions
    application.state.sync_manager = manager
    application.state.metering = metering
    application.state.rating = rating
    application.state.policy = load_policy(settings.billing_policy_path)
    application.include_router(internal_router)
    application.include_router(invoicing_router)
    application.include_router(router)
    application.include_router(metering_router)
    application.include_router(pricing_router)
    application.include_router(rating_router)
    application.mount("/static", StaticFiles(directory=STATIC), name="static")

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            # Handle before Uvicorn can log exception text from an SDK/DB response.
            event("api_failed", code="internal_error")
            response = JSONResponse(status_code=500, content={"detail": "Internal application error"})
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/" or request.url.path.startswith(
            (
                "/projects/",
                "/history",
                "/resources/",
                "/quality",
                "/metering-runs",
                "/costs",
                "/pricing",
                "/rating-runs",
                "/billing-cycles",
                "/invoices",
                "/adjustments",
            )
        ):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
            )
        return response

    @application.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        event("api_failed", code="internal_error")
        return JSONResponse(
            status_code=500, content={"detail": "Internal error; check structured application logs"}
        )

    @application.get("/", include_in_schema=False)
    @application.get("/sync", include_in_schema=False)
    @application.get("/projects/{project_id}", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC / "index.html")

    @application.get("/history", include_in_schema=False)
    @application.get("/resources/{resource_type}/{resource_id}", include_in_schema=False)
    @application.get("/quality", include_in_schema=False)
    @application.get("/metering-runs", include_in_schema=False)
    def historical_dashboard():
        return FileResponse(STATIC / "history.html")

    @application.get("/costs", include_in_schema=False)
    @application.get("/pricing", include_in_schema=False)
    @application.get("/rating-runs", include_in_schema=False)
    def financial_dashboard():
        return FileResponse(STATIC / "costs.html")

    @application.get("/billing-cycles", include_in_schema=False)
    @application.get("/invoices", include_in_schema=False)
    @application.get("/invoices/{identifier}", include_in_schema=False)
    @application.get("/adjustments", include_in_schema=False)
    def invoice_dashboard():
        return FileResponse(STATIC / "billing.html")

    return application


app = create_app()
