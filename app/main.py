"""FastAPI application: calibration registry, deactivation and order manifests.

The same handlers are mounted under several resource-name aliases so the
service is easy to drive from scripts or an agent.  Canonical names:

* ``POST/GET  /api/calibration-files``
* ``GET       /api/calibration-files/{id}``
* ``POST      /api/calibration-files/{id}/deactivate``
* ``POST/GET  /api/orders``
* ``GET       /api/orders/{id}``
* ``POST      /api/orders/{id}/publish``
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .db import Database
from .planner import MAX_CANDIDATES, normalize_kind
from .service import (
    ApiError,
    create_order,
    preview_order,
    publish_order,
    recompute_order,
    register_file,
    resolve_file,
    serialize_file,
    serialize_order,
    truthy,
    utcnow,
)

FILE_PREFIXES = [
    "/api/calibration-files", "/calibration-files",
    "/api/calibrations", "/calibrations",
    "/api/files", "/files",
    "/api/calibration", "/calibration",
]
ORDER_PREFIXES = [
    "/api/orders", "/orders",
    "/api/order", "/order",
]
DEACTIVATION_PATHS = [
    "/api/deactivations", "/deactivations",
    "/api/deactivation", "/deactivation",
]
PLAN_PATHS = [
    "/api/plan", "/plan",
    "/api/match", "/match",
    "/api/manifests", "/manifests",
    "/api/manifest", "/manifest",
]


def _bool_query(value: str | None) -> bool | None:
    if value is None:
        return None
    v = value.strip().lower()
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    return None


FILE_BODY_EXTRA: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["kind", "detector", "batch_id", "size_bytes"],
                    "properties": {
                        "id": {"description": "Optional client id (integer or business code)",
                               "oneOf": [{"type": "integer"}, {"type": "string"}]},
                        "kind": {"type": "string", "enum": ["dark", "flat", "暗场", "平场"]},
                        "detector": {"type": "string", "example": "CCD-A"},
                        "exposure_seconds": {"type": "number", "example": 60},
                        "filter": {"type": "string", "example": "r"},
                        "batch_id": {"type": "string", "example": "B2026-11-25"},
                        "size_bytes": {"type": "integer", "minimum": 0, "example": 10485760},
                        "is_active": {"type": "boolean", "default": True},
                    },
                },
                "examples": {
                    "dark": {"value": {"kind": "dark", "detector": "CCD-A",
                                       "exposure_seconds": 60, "batch_id": "B1",
                                       "size_bytes": 100000000}},
                    "flat": {"value": {"kind": "flat", "detector": "CCD-A", "filter": "r",
                                       "batch_id": "B1", "size_bytes": 100000000}},
                    "bulk": {"value": [
                        {"kind": "dark", "detector": "CCD-A", "exposure_seconds": 60,
                         "batch_id": "B1", "size_bytes": 1},
                        {"kind": "flat", "detector": "CCD-A", "filter": "r",
                         "batch_id": "B1", "size_bytes": 2}]},
                },
            }
        },
    }
}

ORDER_BODY_EXTRA: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["images", "candidate_file_ids"],
                    "properties": {
                        "images": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "image_id": {"type": "string"},
                                    "detector": {"type": "string"},
                                    "exposure_seconds": {"type": "number"},
                                    "filter": {"type": "string"},
                                },
                                "required": ["image_id", "detector"],
                            },
                        },
                        "candidate_file_ids": {
                            "type": "array",
                            "maxItems": 16,
                            "items": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
                        },
                        "publish": {"type": "boolean",
                                    "description": "Compute and atomically publish when complete"},
                    },
                },
                "example": {
                    "images": [
                        {"image_id": "M42-001", "detector": "CCD-A",
                         "exposure_seconds": 60, "filter": "r"},
                        {"image_id": "M42-002", "detector": "CCD-A",
                         "exposure_seconds": 60, "filter": "r"},
                    ],
                    "candidate_file_ids": [1, 2, 3, 4],
                },
            }
        },
    }
}

DEACTIVATE_BODY_EXTRA: dict[str, Any] = {
    "requestBody": {
        "required": False,
        "content": {"application/json": {"schema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "example": "hot columns"}},
        }}},
    }
}


async def _json_body(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Endpoint handlers
# ---------------------------------------------------------------------------

async def health(request: Request) -> dict[str, Any]:
    db: Database = request.app.state.db
    with db.connect() as conn:
        conn.execute("SELECT 1").fetchone()
    return {
        "status": "ok",
        "health": "healthy",
        "healthy": True,
        "service": "astro-calibration-planner",
        "max_candidates_per_order": MAX_CANDIDATES,
        "time": utcnow(),
    }


async def root_info(request: Request) -> dict[str, Any]:
    return {
        "service": "astro-calibration-planner",
        "version": "1.0.0",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "workflow": [
            "POST /api/calibration-files  (register dark/flat files)",
            "POST /api/orders             (submit images + <=16 candidate ids)",
            "POST /api/orders/{id}/publish (revalidate, recompute if needed, freeze manifest)",
            "GET  /api/orders/{id}         (returns the issued frozen manifest)",
            "POST /api/calibration-files/{id}/deactivate",
        ],
        "endpoints": {
            "register_file": "POST /api/calibration-files",
            "list_files": "GET /api/calibration-files",
            "deactivate_file": "POST /api/calibration-files/{id}/deactivate",
            "create_order": "POST /api/orders  (?publish=true to publish atomically)",
            "publish_order": "POST /api/orders/{id}/publish",
            "get_order": "GET /api/orders/{id}",
        },
    }


def _create_file_from_body(db: Database, body: Any) -> Any:
    body = _unwrap_envelope(
        body,
        ("file", "calibration_file", "calibration", "record", "data"),
        ("kind", "type", "file_type", "detector", "batch_id", "batch", "size_bytes", "bytes"),
    )

    if isinstance(body, list):
        created = [register_file(db, item) for item in body]
        return {"created": created, "items": created, "files": created,
                "ids": [c["id"] for c in created], "count": len(created)}

    if isinstance(body, dict):
        for key in ("files", "calibration_files", "calibrations", "items", "records"):
            if isinstance(body.get(key), list):
                created = [register_file(db, item) for item in body[key]]
                return {"created": created, "items": created, "files": created,
                        "ids": [c["id"] for c in created], "count": len(created)}
        return register_file(db, body)

    raise ApiError(422, "payload must be a calibration file object or a list of them")


async def create_file_route(request: Request) -> Any:
    return _create_file_from_body(request.app.state.db, await _json_body(request))


def make_typed_file_route(forced_kind: str):
    async def route(request: Request) -> Any:
        body = await _json_body(request)
        if isinstance(body, dict):
            body.setdefault("kind", forced_kind)
            body.setdefault("type", forced_kind)
        elif isinstance(body, list):
            for item in body:
                if isinstance(item, dict):
                    item.setdefault("kind", forced_kind)
        return _create_file_from_body(request.app.state.db, body)

    route.__name__ = f"create_{forced_kind}_route"
    return route


def make_typed_list_route(forced_kind: str):
    async def route(request: Request) -> list[dict[str, Any]]:
        rows = request.app.state.db.list_files(kind=forced_kind)
        return [serialize_file(r) for r in rows]

    route.__name__ = f"list_{forced_kind}_route"
    return route


async def list_files_route(request: Request) -> list[dict[str, Any]]:
    db: Database = request.app.state.db
    qp = request.query_params
    active = _bool_query(qp.get("active"))
    if active is None and qp.get("status"):
        status = qp["status"].strip().lower()
        if status in {"inactive", "deactivated", "disabled", "retired"}:
            active = False
        elif status in {"active", "in_service", "enabled"}:
            active = True
    kind = normalize_kind(qp.get("kind") or qp.get("type")) if qp.get("kind") or qp.get("type") else None
    detector = qp.get("detector")
    rows = db.list_files(active=active, kind=kind, detector=detector)
    return [serialize_file(r) for r in rows]


async def get_file_route(request: Request, file_id: str) -> dict[str, Any]:
    db: Database = request.app.state.db
    row = resolve_file(db, file_id)
    if row is None:
        raise ApiError(404, f"calibration file {file_id!r} not found", file_id=file_id)
    return serialize_file(row)


def _unwrap_envelope(body: Any, wrapper_keys: tuple[str, ...], content_keys: tuple[str, ...]) -> Any:
    """Unwrap bodies like ``{"order": {...}}`` / ``{"file": {...}}``."""
    if not isinstance(body, dict):
        return body
    if any(k in body for k in content_keys):
        return body
    for key in wrapper_keys:
        inner = body.get(key)
        if isinstance(inner, (dict, list)):
            return inner
    return body


async def _change_active(request: Request, file_id: str, active: bool) -> dict[str, Any]:
    db: Database = request.app.state.db
    row = resolve_file(db, file_id)
    if row is None:
        raise ApiError(404, f"calibration file {file_id!r} not found", file_id=file_id)
    body = await _json_body(request)
    reason = request.query_params.get("reason")
    if isinstance(body, dict):
        reason = reason or body.get("reason") or body.get("deactivate_reason") or body.get("note") or body.get("原因")
    action = "deactivated" if not active else "reactivated"
    updated, changed = db.set_active(row["id"], active, action, reason, utcnow())
    result = serialize_file(updated)
    result["changed"] = changed
    return result


async def deactivate_file_route(request: Request, file_id: str) -> dict[str, Any]:
    return await _change_active(request, file_id, False)


async def activate_file_route(request: Request, file_id: str) -> dict[str, Any]:
    return await _change_active(request, file_id, True)


async def delete_file_route(request: Request, file_id: str) -> dict[str, Any]:
    # DELETE is a soft delete (= deactivation) so the audit trail stays intact.
    return await _change_active(request, file_id, False)


async def update_file_route(request: Request, file_id: str) -> dict[str, Any]:
    """PATCH/PUT alias: ``{"is_active": false, "reason": "..."}`` deactivates."""
    db: Database = request.app.state.db
    row = resolve_file(db, file_id)
    if row is None:
        raise ApiError(404, f"calibration file {file_id!r} not found", file_id=file_id)
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise ApiError(422, "JSON body required")
    active: bool | None = None
    if "is_active" in body or "active" in body or "enabled" in body:
        flag = body.get("is_active", body.get("active", body.get("enabled")))
        active = flag if isinstance(flag, bool) else str(flag).lower() in {"1", "true", "yes", "on"}
    status_value = body.get("status") or body.get("state")
    if active is None and status_value is not None:
        s = str(status_value).strip().lower()
        if s in {"inactive", "deactivated", "disabled", "retired", "off", "停用"}:
            active = False
        elif s in {"active", "in_service", "enabled", "on", "在用"}:
            active = True
    if active is not None:
        reason = body.get("reason") or body.get("deactivate_reason")
        action = "reactivated" if active else "deactivated"
        updated, changed = db.set_active(row["id"], active, action, reason, utcnow())
        result = serialize_file(updated)
        result["changed"] = changed
        return result
    return serialize_file(row)


async def deactivate_by_body_route(request: Request) -> dict[str, Any]:
    db: Database = request.app.state.db
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise ApiError(422, "JSON body with file id is required")
    token = body.get("file_id") or body.get("id") or body.get("fileId") or body.get("code")
    if token is None:
        raise ApiError(422, "field 'file_id' is required")
    row = resolve_file(db, token)
    if row is None:
        raise ApiError(404, f"calibration file {token!r} not found", file_id=token)
    reason = body.get("reason") or body.get("deactivate_reason") or body.get("note")
    updated, changed = db.set_active(row["id"], False, "deactivated", reason, utcnow())
    result = serialize_file(updated)
    result["changed"] = changed
    return result


async def list_deactivations_route(request: Request) -> list[dict[str, Any]]:
    db: Database = request.app.state.db
    action = request.query_params.get("action") or "deactivated"
    rows = db.list_status_events(action=None if action == "all" else action)
    return [dict(r) for r in rows]


async def file_history_route(request: Request, file_id: str) -> list[dict[str, Any]]:
    db: Database = request.app.state.db
    row = resolve_file(db, file_id)
    if row is None:
        raise ApiError(404, f"calibration file {file_id!r} not found", file_id=file_id)
    with db.connect() as conn:
        events = conn.execute(
            "SELECT * FROM file_status_events WHERE file_id = ? ORDER BY id", (row["id"],)
        ).fetchall()
    return [dict(e) for e in events]


async def create_deactivation_route(request: Request) -> dict[str, Any]:
    return await deactivate_by_body_route(request)


async def create_order_route(request: Request) -> dict[str, Any]:
    db: Database = request.app.state.db
    body = await _json_body(request)
    body = _unwrap_envelope(
        body,
        ("order", "order_request", "payload", "request", "data"),
        ("images", "frames", "raw_images", "candidate_file_ids", "candidate_ids"),
    )
    qp_publish = _bool_query(request.query_params.get("publish") or request.query_params.get("auto_publish"))
    strict = bool(_bool_query(request.query_params.get("strict")))
    return create_order(db, body, force_publish=qp_publish, strict=strict)


async def list_orders_route(request: Request) -> list[dict[str, Any]]:
    db: Database = request.app.state.db
    return [serialize_order(r) for r in db.list_orders()]


async def get_order_route(request: Request, order_id: str) -> dict[str, Any]:
    db: Database = request.app.state.db
    row = db.get_order(order_id)
    if row is None:
        raise ApiError(404, f"order {order_id!r} not found", order_id=order_id)
    return preview_order(db, row)


async def publish_order_route(request: Request, order_id: str) -> dict[str, Any]:
    db: Database = request.app.state.db
    strict = bool(_bool_query(request.query_params.get("strict")))
    body = await _json_body(request)
    if isinstance(body, dict):
        strict = strict or truthy(body.get("strict")) or truthy(body.get("raise_on_missing"))
    return publish_order(db, order_id, strict=strict)


async def recompute_order_route(request: Request, order_id: str) -> dict[str, Any]:
    db: Database = request.app.state.db
    return recompute_order(db, order_id)


async def publish_by_body_route(request: Request) -> dict[str, Any]:
    """``POST /api/publish`` with ``{"order_id": 1}``."""
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise ApiError(422, "JSON body with order_id is required")
    order_id = body.get("order_id") or body.get("id") or body.get("orderId") or body.get("order_no")
    if order_id is None:
        raise ApiError(422, "field 'order_id' is required")
    strict = truthy(body.get("strict"))
    return publish_order(request.app.state.db, order_id, strict=strict)


async def update_order_route(request: Request, order_id: str) -> dict[str, Any]:
    """PATCH/PUT an order; ``{"publish": true}`` publishes it."""
    db: Database = request.app.state.db
    body = await _json_body(request)
    if isinstance(body, dict):
        wants_publish = truthy(body.get("publish")) or truthy(body.get("auto_publish"))
        status_value = str(body.get("status") or body.get("state") or "").strip().lower()
        action = str(body.get("action") or "").strip().lower()
        if wants_publish or status_value in {"published", "publish", "issued", "issue"} or action == "publish":
            return publish_order(db, order_id, strict=truthy(body.get("strict")))
        if action in {"recompute", "recalculate", "calculate"}:
            return recompute_order(db, order_id)
    row = db.get_order(order_id)
    if row is None:
        raise ApiError(404, f"order {order_id!r} not found", order_id=order_id)
    return preview_order(db, row)


async def get_manifest_route(request: Request, order_id: str) -> dict[str, Any]:
    db: Database = request.app.state.db
    row = db.get_order(order_id)
    if row is None:
        raise ApiError(404, f"order {order_id!r} not found", order_id=order_id)
    data = serialize_order(row)
    if not data["published"]:
        raise ApiError(
            409,
            "order is not published yet",
            order=data,
            missing_images=data.get("missing_image_ids", []),
            publishable=data.get("publishable", False),
        )
    return data["manifest"]


async def order_events_route(request: Request, order_id: str) -> list[dict[str, Any]]:
    db: Database = request.app.state.db
    row = db.get_order(order_id)
    if row is None:
        raise ApiError(404, f"order {order_id!r} not found", order_id=order_id)
    return [dict(r) for r in db.list_order_events(row["id"])]


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("CALIB_DB_PATH") or os.environ.get("SQLITE_PATH") or "data/calibration.db"
    db = Database(db_path)

    app = FastAPI(
        title="Astro Calibration Planner",
        version="1.0.0",
        description=(
            "Registers dark/flat calibration files in SQLite (with deactivation "
            "audit), builds minimum-download manifests for observation orders "
            "(<=16 candidate files), and freezes issued manifests."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.db = db

    @app.exception_handler(ApiError)
    async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        body: dict[str, Any] = {"detail": exc.message, "error": exc.message, "status_code": exc.status_code}
        body.update(exc.extra)
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(Exception)
    async def _unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"detail": "internal server error", "error": str(exc)})

    # Health / meta
    app.add_api_route("/health", health, methods=["GET"])
    app.add_api_route("/api/health", health, methods=["GET"])
    app.add_api_route("/healthz", health, methods=["GET"])
    app.add_api_route("/ready", health, methods=["GET"])
    app.add_api_route("/", root_info, methods=["GET"])

    _op_counter = {"n": 0}

    def add(path: str, endpoint: Any, methods: list[str],
            openapi_extra: dict[str, Any] | None = None) -> None:
        """Register a route plus its trailing-slash twin with unique operation ids."""
        paths = [path] if path == "/" or path.endswith("/") else [path, path + "/"]
        for one_path in paths:
            for method in methods:
                _op_counter["n"] += 1
                operation_id = f"{endpoint.__name__}_{_op_counter['n']:03d}_{method.lower()}"
                app.add_api_route(
                    one_path, endpoint, methods=[method],
                    operation_id=operation_id, openapi_extra=openapi_extra,
                )

    # Calibration file resources under every common name.
    for prefix in FILE_PREFIXES:
        add(prefix, create_file_route, ["POST", "PUT"], FILE_BODY_EXTRA)
        add(prefix, list_files_route, ["GET"])
        add(f"{prefix}/deactivate", deactivate_by_body_route, ["POST"], DEACTIVATE_BODY_EXTRA)
        add(f"{prefix}/deactivations", list_deactivations_route, ["GET"])
        add(f"{prefix}/deactivate/{{file_id}}", deactivate_file_route, ["POST"], DEACTIVATE_BODY_EXTRA)
        add(f"{prefix}/{{file_id}}", get_file_route, ["GET"])
        add(f"{prefix}/{{file_id}}", update_file_route, ["POST", "PATCH", "PUT"], DEACTIVATE_BODY_EXTRA)
        add(f"{prefix}/{{file_id}}/status", update_file_route, ["POST", "PUT", "PATCH"], DEACTIVATE_BODY_EXTRA)
        add(f"{prefix}/{{file_id}}", delete_file_route, ["DELETE"])
        add(f"{prefix}/{{file_id}}/deactivate", deactivate_file_route, ["POST", "PUT"], DEACTIVATE_BODY_EXTRA)
        add(f"{prefix}/{{file_id}}/disable", deactivate_file_route, ["POST"], DEACTIVATE_BODY_EXTRA)
        add(f"{prefix}/{{file_id}}/retire", deactivate_file_route, ["POST"], DEACTIVATE_BODY_EXTRA)
        add(f"{prefix}/{{file_id}}/active-false", deactivate_file_route, ["POST"], DEACTIVATE_BODY_EXTRA)
        add(f"{prefix}/{{file_id}}/activate", activate_file_route, ["POST", "PUT"])
        add(f"{prefix}/{{file_id}}/enable", activate_file_route, ["POST"])
        add(f"{prefix}/{{file_id}}/deactivations", file_history_route, ["GET"])
        add(f"{prefix}/{{file_id}}/history", file_history_route, ["GET"])
        add(f"{prefix}/{{file_id}}/events", file_history_route, ["GET"])

    # Deactivation audit resources.
    for path in DEACTIVATION_PATHS:
        add(path, list_deactivations_route, ["GET"])
        add(path, create_deactivation_route, ["POST"], DEACTIVATE_BODY_EXTRA)

    # Kind-specific collection aliases: POST /darks, /flats, ...
    for kind, words in (("dark", ("darks", "dark", "dark-frames", "darkframes")),
                        ("flat", ("flats", "flat", "flat-frames", "flatframes"))):
        creator = make_typed_file_route(kind)
        lister = make_typed_list_route(kind)
        for word in words:
            for path in (f"/{word}", f"/api/{word}"):
                add(path, creator, ["POST"], FILE_BODY_EXTRA)
                add(path, lister, ["GET"])

    # Orders.
    for prefix in ORDER_PREFIXES:
        add(prefix, create_order_route, ["POST", "PUT"], ORDER_BODY_EXTRA)
        add(f"{prefix}/submit", create_order_route, ["POST"], ORDER_BODY_EXTRA)
        add(f"{prefix}/publish", publish_by_body_route, ["POST", "PUT"])
        add(prefix, list_orders_route, ["GET"])
        add(f"{prefix}/{{order_id}}", get_order_route, ["GET"])
        add(f"{prefix}/{{order_id}}", update_order_route, ["PATCH", "PUT"])
        for action in ("publish", "release", "issue", "finalize", "confirm"):
            add(f"{prefix}/{{order_id}}/{action}", publish_order_route, ["POST", "PUT"])
        for action in ("recompute", "recalculate", "refresh", "replan", "calculate", "compute", "match"):
            add(f"{prefix}/{{order_id}}/{action}", recompute_order_route, ["POST", "PUT"])
        add(f"{prefix}/{{order_id}}/manifest", get_manifest_route, ["GET"])
        add(f"{prefix}/{{order_id}}/result", get_manifest_route, ["GET"])
        add(f"{prefix}/{{order_id}}/events", order_events_route, ["GET"])

    # Body-driven publish aliases at the top level.
    for path in ("/api/publish", "/publish", "/api/issue", "/issue"):
        add(path, publish_by_body_route, ["POST", "PUT"])

    # Manifest/plan-style resources behave like order creation.
    for path in PLAN_PATHS:
        add(path, create_order_route, ["POST"], ORDER_BODY_EXTRA)

    return app


app = create_app()
