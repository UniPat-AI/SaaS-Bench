"""FastAPI HTTP adapter over ``PlaygroundService`` (the in-cluster service).

Thin: maps the prepare/grade/release wire contract to the service core and ``ServiceError`` -> HTTP
status. ``fastapi`` is imported lazily inside ``create_app`` so the module can ``py_compile``
without it. Pydantic models are defined at MODULE LEVEL — Pydantic v2 won't generate the OpenAPI
schema for models defined inside a closure (it can't resolve their ForwardRef). Pydantic is a
transitive fastapi dependency, so any environment that runs the service already has it.

The typed request/response models below are what OpenAPI exports at ``/openapi.json`` (Swagger UI
at ``/docs``), so portal integrators can codegen a Ruby/Go/JS client from the spec.

Wire up in a deploy entrypoint, e.g.::

    service = PlaygroundService(catalog=load_catalog(), provisioner=HelmProvisioner(...))
    app = create_app(service, api_key=os.environ["PLAYGROUND_API_KEY"])
"""

import logging
import os
import re
import secrets
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from saas_bench.playground_service import PlaygroundService, ServiceError

# Do not enable postponed annotations in this module. ``Request`` is imported lazily inside
# ``create_app``; FastAPI needs the concrete class on each handler, otherwise it interprets the
# string annotation as a required query parameter (POST /runs -> 422 `query.request missing`).

# X-Request-Id: trust an inbound value only if it looks well-formed (printable, bounded length).
# Anything else, we mint a fresh one — defends against header-injection (newlines into logs) and
# pathologically large values without forcing a UUID format on every integrator.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{4,64}$")
_log = logging.getLogger("saas_bench.playground_api")

# OpenAPI/docs paths are exempt from bearer so portal devs can browse the spec without a key.
# Everything else (prepare/grade/release) requires Authorization: Bearer <key>.
_PUBLIC_PATHS = {
    "/healthz",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/docs/oauth2-redirect",
}

_DESCRIPTION = """\
Provision and ground-truth-grade per-run SaaS-app environments for computer-use-agent evals.

**Lifecycle:** `POST /runs` (start async prepare for a fresh per-run env) →
poll `GET /runs/{run_id}` until `status=prepared` and `public_ready=true` →
agent acts on the returned `access_urls[<app>]` →
`POST /grade` (server-side runs the unmodified `verify.py` and returns a weighted score) →
`POST /release` (tear down). All calls require `Authorization: Bearer <key>` except `/healthz`.

**Latency:** Helm and GKE node scale-up can be slow. Use the async `POST /runs` contract for UI
flows so no browser or proxy has to hold one long request open. `POST /prepare` remains as a
backward-compatible synchronous endpoint for batch callers that intentionally want blocking
behavior.

**Retry semantics:** `grade` and `release` are server-side idempotent — retry them on 408/429/5xx
(incl. Cloudflare 52x). `POST /runs` is also idempotent when the caller reuses the same
`X-Request-Id`; recover a lost response with `GET /runs?request_id=…`. The legacy synchronous
`POST /prepare` remains single-shot. See `docs/playground-api-integration.md` for the full guide.

**Prompt construction:** non-Python integrators (e.g. a Ruby portal) call
`GET /tasks/{task_id}/prompt?run_id=…` after `prepare` to receive the fully-rendered agent prompt
with per-run URLs already substituted. Avoids reimplementing `loader.build_prompt` outside Python.

**Tracing:** every response carries `X-Request-Id`. Send your own (4–64 chars, `[A-Za-z0-9_-]`)
to correlate playground logs with portal audit rows; the service mints one if absent.
"""


# ---- request / response models (module-level so Pydantic can introspect them for /openapi.json) -

class PrepareRequest(BaseModel):
    task_id: str = Field(..., description="Curated task ID (must be in the service catalog).")
    expected_manifest_digest: str = Field(
        "", description=(
            "If non-empty, the service rejects with 409 unless this matches its catalog digest. "
            "Use this to fail fast on client/server version drift; pass \"\" to skip."
        ),
    )


class PrepareResponse(BaseModel):
    run_id: str = Field(..., description="Unique handle for this provisioned env. Use it for grade/release.")
    status: str = Field("prepared", description="Always `prepared` for the synchronous prepare endpoint.")
    access_urls: Dict[str, str] = Field(
        ..., description=(
            "Per-app public URL (multi-app tasks return one entry per app). The agent navigates "
            "to these over HTTPS; legacy /prepare callers must poll until a non-404/5xx response."
        ),
    )
    verify_context: Dict[str, str] = Field(
        ..., description=(
            "Env the grader needs (e.g. ``<APP>_CONTAINER``, ``<APP>_PORT``, ``<APP>_DB_CONTAINER``). "
            "Opaque to the portal; the service retains it with the run."
        ),
    )
    task_version: str = Field(
        ..., description="12-char digest of the task's description + meta + verify.py (for drift detection).",
    )


class RunStatusResponse(BaseModel):
    run_id: str
    request_id: str = Field("", description="Validated X-Request-Id associated with POST /runs.")
    task_id: str
    status: str = Field(..., description="`preparing`, `prepared`, or `failed`.")
    access_urls: Dict[str, str] = Field(default_factory=dict)
    app_reachability: Dict[str, bool] = Field(
        default_factory=dict,
        description="Sticky per-app public-edge probe results; meaningful once status is prepared.",
    )
    public_ready: bool = Field(
        False,
        description="True when every access URL has answered through its public hostname.",
    )
    verify_context: Dict[str, str] = Field(default_factory=dict)
    task_version: str = Field("", description="Task version recorded when the run was prepared.")
    current_task_version: str = Field("", description="Task version in the serving platform image.")
    task_version_changed: bool = Field(
        False,
        description="True when the serving task changed after this run was prepared.",
    )
    error: Optional[str] = None
    status_url: Optional[str] = Field(None, description="Relative URL to poll for this run.")


class GradeRequest(BaseModel):
    task_id: str
    run_id: str


class GradeCheck(BaseModel):
    label: str
    weight: int
    passed: bool
    status: str = Field(
        "",
        description="``PASS`` | ``FAIL`` | ``ERROR``; empty only for legacy grade payloads.",
    )
    detail: str = ""


class GradeResponse(BaseModel):
    task_id: str
    task_version: str = Field("", description="Task version whose verifier produced this grade.")
    prepared_task_version: str = Field(
        "", description="Task version recorded when the graded run was prepared.",
    )
    status: str = Field(..., description="``PASS`` | ``FAIL`` | ``ERROR`` | ``SKIP``.")
    score: float = Field(..., description="``earned / total``, 0-1.")
    earned: int = 0
    total: int = 0
    all_pass: bool = False
    checks: List[GradeCheck] = Field(default_factory=list)
    returncode: Optional[int] = None
    error: Optional[str] = None
    warnings: List[str] = Field(
        default_factory=list,
        description="Non-fatal grading context, such as task-version drift or shared audit mode.",
    )


class ReleaseRequest(BaseModel):
    run_id: str


class ReleaseResponse(BaseModel):
    released: bool = Field(
        ...,
        description="True if the env existed and was torn down; False if the run_id was unknown (idempotent).",
    )


class HealthResponse(BaseModel):
    ok: bool


class ErrorResponse(BaseModel):
    error: str


class CatalogEntry(BaseModel):
    task_id: str
    sites: List[str] = Field(..., description="The apps this task spans (drives prepare's provisioning).")
    task_version: str = Field(..., description="12-char digest of the task content (description+meta+verify.py).")
    description: str = Field("", description="First ~280 chars of the task description, for portal UX.")


class CatalogResponse(BaseModel):
    tasks: List[CatalogEntry]


class MultimodalFile(BaseModel):
    name: str = Field(..., description="Filename (basename only; server filesystem path is not exposed).")
    path: str = Field(
        ...,
        description=(
            "Absolute path *inside the playground container* — opaque to the portal. Diagnostic "
            "only; fetch ``url`` to obtain the bytes."
        ),
    )
    url: str = Field(
        "",
        description=(
            "Service-relative path to download the attachment's bytes "
            "(``GET /tasks/{task_id}/files/{name}``, same bearer auth as the rest of the API). "
            "The agent sandbox cannot read the playground's filesystem, so a runner MUST fetch "
            "this and place the file in the agent's workspace — otherwise the task is graded "
            "against an asset the agent never received."
        ),
    )


class PromptResponse(BaseModel):
    task_id:      str
    run_id:       str
    task_version: str = Field(..., description="12-char digest of the task content (matches /catalog).")
    prepared_task_version: str = Field(
        "", description="Task version recorded when this run was prepared.",
    )
    prompt: str = Field(
        ...,
        description=(
            "Full agent prompt with the per-run ``access_urls`` already substituted. Hand to the "
            "agent verbatim — do not re-template URLs on the portal side."
        ),
    )
    todo_md: str = Field(
        ...,
        description="Pre-filled todo.md derived from the task's **Steps:** list (the agent may revise).",
    )
    multimodal_input_files: List[MultimodalFile] = Field(
        default_factory=list,
        description=(
            "Any multimodal attachments declared in the task's meta.json. Names only today; "
            "fetching bytes is M1+ via a sibling endpoint."
        ),
    )


def create_app(service: PlaygroundService, api_key: str = ""):
    from fastapi import FastAPI, Request                    # lazy: not needed for module import
    from fastapi.responses import FileResponse, JSONResponse

    app = FastAPI(
        title="SaaS-Bench Target Playground",
        version="1.0.0",
        description=_DESCRIPTION,
    )

    @app.middleware("http")
    async def _require_bearer(request: Request, call_next):
        # Gateway exposes this service publicly; prepare/grade/release need auth. Docs/healthz are
        # exempt so portal devs can browse the OpenAPI spec without a key.
        # Bearer scheme is case-insensitive per RFC 7235 — proxies may downcase 'bearer'. Parse the
        # scheme separately and use compare_digest only on the (secret) token.
        if api_key and request.url.path not in _PUBLIC_PATHS:
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not secrets.compare_digest(token, api_key):
                response = JSONResponse(status_code=401, content={"error": "unauthorized"})
                # Carry the request_id (set by the outer middleware) onto the auth-failure response
                # too, so the portal can correlate 401s with its EvalAuditEvent rows.
                rid = getattr(request.state, "request_id", "")
                if rid:
                    response.headers["x-request-id"] = rid
                return response
        return await call_next(request)

    # Registered LAST so it runs OUTERMOST — request_id is set before bearer check fires and is
    # available on every response (including 401s) for portal correlation with EvalAuditEvent.
    @app.middleware("http")
    async def _request_id(request: Request, call_next):
        inbound = request.headers.get("x-request-id", "")
        rid = inbound if _REQUEST_ID_RE.match(inbound) else secrets.token_hex(8)
        request.state.request_id = rid
        t0 = time.monotonic()
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        # One line per request — enough for grep-by-rid; uvicorn's own access log carries the body.
        _log.info(
            "rid=%s method=%s path=%s status=%d dur_ms=%d",
            rid, request.method, request.url.path, response.status_code,
            int((time.monotonic() - t0) * 1000),
        )
        return response

    @app.exception_handler(ServiceError)
    async def _service_error_handler(_request: Request, exc: ServiceError):
        return JSONResponse(status_code=exc.status, content={"error": str(exc)})

    # Sync handlers (not `async def`) so FastAPI runs them in its threadpool — the service calls
    # subprocess.run for helm/kubectl which would otherwise block uvicorn's single event loop and
    # starve /healthz (the LB then marks the backend unhealthy mid-provision).
    @app.post(
        "/prepare",
        response_model=PrepareResponse,
        summary="Provision a fresh per-run env for a task",
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
                   409: {"model": ErrorResponse}, 429: {"model": ErrorResponse},
                   503: {"model": ErrorResponse}},
    )
    def prepare(req: PrepareRequest) -> Any:
        return service.prepare(req.task_id, req.expected_manifest_digest)

    @app.post(
        "/runs",
        response_model=RunStatusResponse,
        status_code=202,
        summary="Start async provisioning for a fresh per-run env",
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
                   409: {"model": ErrorResponse}, 429: {"model": ErrorResponse},
                   503: {"model": ErrorResponse}},
    )
    def start_run(req: PrepareRequest, request: Request) -> Any:
        out = service.start_prepare(
            req.task_id,
            req.expected_manifest_digest,
            request_id=request.state.request_id,
        )
        out["status_url"] = f"/runs/{out['run_id']}"
        return out

    @app.post(
        "/audit/runs",
        response_model=RunStatusResponse,
        status_code=202,
        summary="Start or adopt the shared untouched-app verifier audit pool",
        responses={401: {"model": ErrorResponse}, 409: {"model": ErrorResponse},
                   429: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    def start_audit_pool(request: Request) -> Any:
        out = service.start_audit_pool(request_id=request.state.request_id)
        out["status_url"] = f"/runs/{out['run_id']}"
        return out

    @app.get(
        "/runs",
        response_model=RunStatusResponse,
        summary="Recover an async run by its X-Request-Id",
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
                   503: {"model": ErrorResponse}},
    )
    def run_by_request_id(request_id: str) -> Any:
        out = service.lookup_run(request_id)
        out["status_url"] = f"/runs/{out['run_id']}"
        return out

    @app.get(
        "/runs/{run_id}",
        response_model=RunStatusResponse,
        summary="Poll async prepare status for a per-run env",
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
                   503: {"model": ErrorResponse}},
    )
    def run_status(run_id: str) -> Any:
        out = service.run_status(run_id)
        out["status_url"] = f"/runs/{run_id}"
        return out

    @app.post(
        "/grade",
        response_model=GradeResponse,
        summary="Run verify.py server-side against the per-run env",
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
                   409: {"model": ErrorResponse}, 429: {"model": ErrorResponse},
                   503: {"model": ErrorResponse}},
    )
    def grade(req: GradeRequest) -> Any:
        return service.grade(req.task_id, req.run_id)

    @app.post(
        "/audit/grade",
        response_model=GradeResponse,
        summary="Run one verifier against the shared untouched-app audit pool",
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
                   409: {"model": ErrorResponse}, 429: {"model": ErrorResponse},
                   503: {"model": ErrorResponse}},
    )
    def audit_grade(req: GradeRequest) -> Any:
        return service.grade_from_audit_pool(req.task_id, req.run_id)

    @app.post(
        "/release",
        response_model=ReleaseResponse,
        summary="Tear down the per-run env (idempotent)",
        responses={401: {"model": ErrorResponse}, 409: {"model": ErrorResponse},
                   503: {"model": ErrorResponse}},
    )
    def release(req: ReleaseRequest) -> Any:
        return service.release(req.run_id)

    @app.get(
        "/catalog",
        response_model=CatalogResponse,
        summary="List the tasks this service can provision",
        responses={401: {"model": ErrorResponse}},
    )
    def catalog() -> Any:
        return {"tasks": service.list_catalog()}

    @app.get(
        "/tasks/{task_id}/prompt",
        response_model=PromptResponse,
        summary="Get the agent prompt for a live run, with per-run URLs substituted",
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse},
                   409: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    def task_prompt(task_id: str, run_id: str) -> Any:
        # ``run_id`` is required: the prompt embeds the per-run access_urls. Asking for a
        # prompt without an active run would either return placeholder URLs (footgun) or
        # require a parallel templating path (drift risk) — neither is worth it.
        return service.build_prompt(task_id, run_id)

    @app.get(
        "/tasks/{task_id}/files/{name}",
        summary="Download a task's multimodal attachment (photo/PDF/poster) as bytes",
        response_class=FileResponse,
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    def task_file(task_id: str, name: str) -> Any:
        # The agent sandbox has no access to this container's filesystem, so a task that declares a
        # `multimodal_input` needs its bytes over HTTP or the asset is simply absent for the run.
        # `service.task_file` only resolves names the task itself declares — `name` is matched
        # against that allow-list, never joined onto a path.
        path = service.task_file(task_id, name)
        return FileResponse(path, filename=os.path.basename(path))

    @app.get(
        "/healthz",
        response_model=HealthResponse,
        summary="Liveness probe (unauthenticated)",
    )
    def healthz() -> Any:
        return {"ok": True}

    return app
