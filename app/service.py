"""Business workflow: tolerant input normalisation, order lifecycle, serialisation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .db import Database
from .planner import (
    MAX_CANDIDATES,
    CalibFile,
    ImageSpec,
    build_plan,
    exposure_key,
    normalize_kind,
    norm_text,
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApiError(Exception):
    def __init__(self, status_code: int, message: str, **extra: Any):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra


# ---------------------------------------------------------------------------
# Tolerant key extraction (English aliases + common Chinese names)
# ---------------------------------------------------------------------------

def _lower_map(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(k, str):
            out[k.lower()] = v
            out[k.strip().lower()] = v
    return out


def pick(data: Any, *keys: str) -> Any:
    if not isinstance(data, dict):
        return None
    direct = _lower_map(data)
    for key in keys:
        if key in data:
            return data[key]
        lk = key.lower()
        if lk in direct:
            return direct[lk]
    return None


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "publish", "发布", "是"}


KIND_KEYS = ("kind", "type", "file_type", "filetype", "calibration_type", "cal_type", "frame_type", "frametype", "frame", "calibration_kind", "类型", "文件类型", "标定类型")
DETECTOR_KEYS = ("detector", "detector_id", "detectorid", "detector_name", "camera", "camera_id", "camera_name", "sensor", "sensor_id", "sensor_name", "ccd", "ccd_id", "instrument", "instrument_id", "det", "探测器", "探测器编号")
BATCH_KEYS = ("batch_id", "batchid", "batch", "batch_no", "batchno", "batch_number", "batchnum", "calibration_batch", "calibration_batch_id", "batch_code", "calibration_batch_no", "批次", "标定批次", "批次号", "标定批次号")
BYTES_KEYS = ("size_bytes", "sizebytes", "bytes", "nbytes", "file_size_bytes", "file_size", "filesize", "size", "download_bytes", "download_size", "byte_size", "content_length", "字节", "字节数", "大小", "文件大小", "下载字节数")
EXPOSURE_KEYS = ("exposure_seconds", "exposureseconds", "exposure_time_s", "exposuretime", "exposure_time", "exposure", "exptime", "exp_time", "time_exp", "t_exp", "exp", "exposure_sec", "exposure_s", "曝光时长", "曝光时间", "曝光")
FILTER_KEYS = ("filter_name", "filtername", "filter", "band", "bandpass", "filter_band", "filter_id", "filterid", "filter_name_id", "filter_wheel", "滤镜", "滤光片", "波段", "滤镜编号")
CODE_KEYS = ("code", "file_no", "fileno", "file_number", "filenumber", "number", "no", "bianhao", "编号", "文件编号")
ID_KEYS = ("id", "file_id", "fileid", "pk", "ID".lower())
ACTIVE_KEYS = ("is_active", "active", "enabled", "in_service", "在用", "启用")

IMAGE_LIST_KEYS = ("images", "frames", "raw_images", "rawimages", "raws", "raw_frames", "light_frames", "lights", "targets", "image_list", "exposures", "原图", "图像", "图片", "原图信息", "原图列表")
CANDIDATE_KEYS = ("candidate_file_ids", "candidatefileids", "candidate_ids", "candidateids", "file_ids", "fileids", "candidates", "candidate_files", "candidatefiles", "candidate_file_numbers", "candidate_numbers", "候选", "候选文件", "候选编号", "候选文件编号")
PUBLISH_KEYS = ("publish", "publish_now", "publishnow", "auto_publish", "autopublish", "issue", "release", "发布", "立即发布")
STRICT_KEYS = ("strict", "raise_on_missing", "require_complete")

IMAGE_ID_KEYS = ("image_id", "imageid", "id", "name", "filename", "file_name", "frame_id", "编号", "图像编号", "原图编号", "文件名")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        # "1,2,3" / "D1 D2"
        parts = [p.strip() for chunk in text.split(",") for p in chunk.split()]
        return [p for p in parts if p]
    return [value]


def _id_token(value: Any) -> Any:
    if isinstance(value, dict):
        return pick(value, *ID_KEYS, *CODE_KEYS)
    return value


def _is_int_token(token: Any) -> int | None:
    if isinstance(token, bool):
        return None
    if isinstance(token, int):
        return token
    if isinstance(token, str) and token.strip().isdigit():
        return int(token.strip())
    return None


# ---------------------------------------------------------------------------
# Calibration file registration
# ---------------------------------------------------------------------------

def normalize_calibration_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ApiError(422, "calibration file payload must be a JSON object")

    kind_raw = pick(payload, *KIND_KEYS)
    kind = normalize_kind(kind_raw)
    if kind not in {"dark", "flat"}:
        raise ApiError(
            422,
            "field 'kind' is required and must be 'dark' or 'flat'",
            received_kind=kind_raw,
        )

    detector = norm_text(pick(payload, *DETECTOR_KEYS))
    if not detector:
        raise ApiError(422, "field 'detector' is required")

    batch_id = norm_text(pick(payload, *BATCH_KEYS))
    if not batch_id:
        raise ApiError(422, "field 'batch_id' (same calibration batch) is required")

    bytes_raw = pick(payload, *BYTES_KEYS)
    try:
        size_bytes = int(Decimal(str(bytes_raw)).to_integral_value()) if bytes_raw is not None else None
    except (InvalidOperation, ValueError):
        size_bytes = None
    if size_bytes is None or size_bytes < 0:
        raise ApiError(422, "field 'size_bytes' must be a non-negative integer", received_bytes=bytes_raw)

    exposure_raw = pick(payload, *EXPOSURE_KEYS)
    exp_key = exposure_key(exposure_raw)
    exposure_value: str | None = None if exposure_raw is None else str(exposure_raw).strip()

    filter_name = norm_text(pick(payload, *FILTER_KEYS)) or None

    # Identity: explicit numeric id -> primary key; otherwise code/business no.
    id_token = pick(payload, *ID_KEYS)
    code_token = pick(payload, "code", "file_no", "fileNo", "file_number", "fileNumber", "bianhao", "编号", "文件编号")
    explicit_id = _is_int_token(id_token)
    code: str | None = None

    if id_token is not None and explicit_id is None:
        code = str(id_token).strip()
    if code_token is not None and str(code_token).strip():
        ct = str(code_token).strip()
        ct_int = _is_int_token(ct)
        if explicit_id is None and id_token is None and ct_int is not None:
            explicit_id = ct_int
        else:
            code = ct
    if explicit_id is not None and id_token is None and code_token is not None and not str(code_token).strip().isdigit():
        code = str(code_token).strip()

    active_raw = pick(payload, *ACTIVE_KEYS)
    is_active = True if active_raw is None else truthy(active_raw) if not isinstance(active_raw, bool) else active_raw

    return {
        "kind": kind,
        "detector": detector,
        "batch_id": batch_id,
        "size_bytes": size_bytes,
        "exposure_key": exp_key,
        "exposure_value": exposure_value,
        "filter_name": filter_name,
        "explicit_id": explicit_id,
        "code": code,
        "is_active": is_active,
    }


def register_file(db: Database, payload: dict[str, Any]) -> dict[str, Any]:
    data = normalize_calibration_payload(payload)
    now = utcnow()
    try:
        row = db.insert_calibration_file(data, now)
    except Exception as exc:  # sqlite IntegrityError etc.
        msg = str(exc)
        if "UNIQUE" in msg:
            raise ApiError(409, "a calibration file with the same id/code already exists", code=data.get("code"), explicit_id=data.get("explicit_id"))
        raise
    if not data["is_active"]:
        row, _ = db.set_active(row["id"], False, "deactivated", "created_inactive", now)
    return serialize_file(row)


def resolve_file(db: Database, token: Any) -> Any | None:
    if token is None:
        return None
    if isinstance(token, int) and not isinstance(token, bool):
        return db.get_file_by_pk(token)
    text = str(token).strip()
    if not text:
        return None
    numeric = _is_int_token(text)
    if numeric is not None:
        row = db.get_file_by_pk(numeric)
        if row is not None:
            return row
    return db.get_file_by_code(text)


def api_file_id(row: Any) -> Any:
    code = row["code"] if "code" in row.keys() else None
    return code if code else row["id"]


def _numericish(value: str | None) -> Any:
    if value is None:
        return None
    try:
        dec = Decimal(value)
        if dec == dec.to_integral_value():
            return int(dec)
        return float(dec)
    except (InvalidOperation, ValueError):
        return value


def serialize_file(row: Any) -> dict[str, Any]:
    d = dict(row)
    api_id = d.get("code") or d["id"]
    is_active = bool(d.get("is_active", 1))
    exposure = _numericish(d.get("exposure_value"))
    return {
        "id": api_id,
        "file_id": api_id,
        "fileId": api_id,
        "pk": d["id"],
        "code": d.get("code"),
        "file_no": d.get("code"),
        "kind": normalize_kind(d.get("kind")) or d.get("kind"),
        "type": normalize_kind(d.get("kind")) or d.get("kind"),
        "detector": d.get("detector"),
        "batch_id": d.get("batch_id"),
        "batchId": d.get("batch_id"),
        "batch": d.get("batch_id"),
        "size_bytes": d.get("size_bytes"),
        "sizeBytes": d.get("size_bytes"),
        "bytes": d.get("size_bytes"),
        "file_size": d.get("size_bytes"),
        "exposure_seconds": exposure,
        "exposureSeconds": exposure,
        "exposure": exposure,
        "filter": d.get("filter_name"),
        "filter_name": d.get("filter_name"),
        "filterName": d.get("filter_name"),
        "is_active": is_active,
        "isActive": is_active,
        "active": is_active,
        "deactivated": not is_active,
        "created_at": d.get("created_at"),
        "deactivated_at": d.get("deactivated_at"),
        "deactivate_reason": d.get("deactivate_reason"),
    }


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def normalize_image(raw: Any, index: int) -> tuple[ImageSpec, dict[str, Any]]:
    if not isinstance(raw, dict):
        image_id = str(raw) if raw is not None else f"img-{index + 1}"
        normalized = {"image_id": image_id, "detector": "", "exposure_seconds": None, "filter": None}
        return ImageSpec(image_id=image_id, detector="", exposure_key=None, filter_name=None, raw=normalized), normalized

    image_id = norm_text(pick(raw, *IMAGE_ID_KEYS)) or f"img-{index + 1}"
    detector = norm_text(pick(raw, *DETECTOR_KEYS))
    exposure_raw = pick(raw, *EXPOSURE_KEYS)
    exp_key = exposure_key(exposure_raw)
    filter_name = norm_text(pick(raw, *FILTER_KEYS)) or None

    normalized = {
        "image_id": image_id,
        "detector": detector,
        "exposure_seconds": exposure_raw,
        "exposure_key": exp_key,
        "filter": filter_name,
    }
    # Preserve every originally supplied field as well.
    merged = {**raw, **{k: v for k, v in normalized.items() if v is not None or k not in raw}}
    normalized["raw"] = raw
    spec = ImageSpec(
        image_id=image_id,
        detector=detector,
        exposure_key=exp_key,
        filter_name=filter_name,
        exposure_seconds=exposure_raw,
        raw=merged,
    )
    return spec, normalized


def _item_dict(assigned: Any) -> dict[str, Any]:
    image: ImageSpec = assigned.image
    dark: CalibFile = assigned.dark
    flat: CalibFile = assigned.flat
    dark_d = serialize_file(dark.raw) if dark.raw else {"id": dark.id}
    flat_d = serialize_file(flat.raw) if flat.raw else {"id": flat.id}
    dark_api = dark_d.get("id", dark.id)
    flat_api = flat_d.get("id", flat.id)
    return {
        "image_id": image.image_id,
        "imageId": image.image_id,
        "image": image.raw,
        "detector": image.detector,
        "exposure_seconds": image.exposure_seconds,
        "exposure_key": image.exposure_key,
        "filter": image.filter_name,
        "batch_id": dark.batch_id,
        "batchId": dark.batch_id,
        "batch": dark.batch_id,
        "dark_file_id": dark_api,
        "darkFileId": dark_api,
        "dark_id": dark_api,
        "dark_pk": dark.id,
        "dark_file": dark_d,
        "dark": dark_d,
        "flat_file_id": flat_api,
        "flatFileId": flat_api,
        "flat_id": flat_api,
        "flat_pk": flat.id,
        "flat_file": flat_d,
        "flat": flat_d,
        "pair_bytes": int(dark.size_bytes or 0) + int(flat.size_bytes or 0),
    }


def make_plan_snapshot(plan: Any, now: str) -> dict[str, Any]:
    items = [_item_dict(a) for a in plan.items]
    files = [serialize_file(f.raw) if f.raw else {"id": f.id, "size_bytes": f.size_bytes} for f in plan.selected_files]
    return {
        "complete": plan.complete,
        "items": items,
        "pairings": items,
        "files": files,
        "selected_pk_ids": plan.selected_ids(),
        "total_bytes": plan.total_bytes,
        "missing_image_ids": [m["image_id"] for m in plan.missing],
        "missing_images": [m["image_id"] for m in plan.missing],
        "missing_image_details": plan.missing,
        "candidate_count": plan.candidate_count,
        "active_count": plan.active_count,
        "computed_at": now,
    }


def make_manifest(order_id: int, order_no: str, snapshot: dict[str, Any], now: str,
                  recomputed: bool, replaced_pk_ids: list[int]) -> dict[str, Any]:
    total = snapshot.get("total_bytes")
    items = snapshot.get("items", [])
    files = snapshot.get("files", [])
    manifest = {
        "manifest_no": f"M-{order_id:06d}",
        "order_id": order_id,
        "orderId": order_id,
        "id": order_id,
        "order_no": order_no,
        "orderNo": order_no,
        "status": "published",
        "published": True,
        "issued_at": now,
        "published_at": now,
        "computed_at": snapshot.get("computed_at"),
        "image_count": len(items),
        "items": items,
        "pairings": items,
        "pairs": items,
        "matches": items,
        "assignments": items,
        "files": files,
        "unique_files": files,
        "total_bytes": total,
        "totalBytes": total,
        "total_download_bytes": total,
        "download_bytes": total,
        "total_size_bytes": total,
        "unique_file_count": len(files),
        "reused_file_savings_note": "files reused across images are counted exactly once",
        "missing_images": [],
        "missing_image_ids": [],
        "recomputed_at_publish": recomputed,
        "recomputed": recomputed,
        "deactivated_selected_file_ids": replaced_pk_ids,
        "replaced_file_ids": replaced_pk_ids,
    }
    return manifest


def create_order(db: Database, payload: Any, force_publish: bool | None = None,
                 strict: bool = False) -> dict[str, Any]:
    if isinstance(payload, list):
        payload = {"images": payload}
    if not isinstance(payload, dict):
        raise ApiError(422, "order payload must be a JSON object")

    images_raw = pick(payload, *IMAGE_LIST_KEYS)
    if images_raw is None:
        images_raw = []
    images_raw_list = _as_list(images_raw)

    candidates_raw = pick(payload, *CANDIDATE_KEYS)
    if candidates_raw is None:
        # Tolerate requests that list dark/flat candidate ids separately.
        split_ids: list[Any] = []
        for key in ("dark_file_ids", "dark_ids", "dark_candidate_ids",
                    "flat_file_ids", "flat_ids", "flat_candidate_ids"):
            value = pick(payload, key)
            if value is not None:
                split_ids.extend(_as_list(value))
        candidates_raw = split_ids or None
    candidate_entries = _as_list(candidates_raw)
    if len(candidate_entries) > MAX_CANDIDATES:
        raise ApiError(
            422,
            f"an order may reference at most {MAX_CANDIDATES} candidate file ids",
            candidate_count=len(candidate_entries),
            max_candidates=MAX_CANDIDATES,
        )

    # Normalise candidate tokens (extract id from objects), dedupe preserve order.
    tokens: list[Any] = []
    seen: set[str] = set()
    for entry in candidate_entries:
        token = _id_token(entry)
        key = repr(token)
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)

    unknown: list[Any] = []
    rows: list[Any] = []
    resolved_pks: list[int] = []
    for token in tokens:
        row = resolve_file(db, token)
        if row is None:
            unknown.append(token)
        else:
            rows.append(row)
            resolved_pks.append(row["id"])
    if unknown:
        raise ApiError(422, "some candidate file ids are not registered", unknown_candidate_ids=unknown)

    specs: list[ImageSpec] = []
    normalized_images: list[dict[str, Any]] = []
    for i, raw_img in enumerate(images_raw_list):
        spec, normalized = normalize_image(raw_img, i)
        specs.append(spec)
        normalized_images.append(normalized)
    if not specs:
        raise ApiError(422, "at least one raw image is required")

    candidates = [CalibFile.from_row(r) for r in rows]
    now = utcnow()
    plan = build_plan(specs, candidates)
    snapshot = make_plan_snapshot(plan, now)

    status = "ready" if plan.complete else "unmatched"
    order_id = db.insert_order(
        now=now,
        status=status,
        request_json=json.dumps(payload, ensure_ascii=False, default=str),
        images_json=json.dumps(normalized_images, ensure_ascii=False, default=str),
        candidate_ids_json=json.dumps(resolved_pks),
        plan_json=json.dumps(snapshot, ensure_ascii=False, default=str),
        total_bytes=plan.total_bytes,
        missing_json=json.dumps(snapshot["missing_image_details"], ensure_ascii=False, default=str),
    )
    order_no = f"ORD-{order_id:06d}"
    db.add_order_event(order_id, "created", json.dumps({"status": status}), now)

    wants_publish = force_publish if force_publish is not None else truthy(pick(payload, *PUBLISH_KEYS))
    is_strict = strict or truthy(pick(payload, *STRICT_KEYS))

    if wants_publish and plan.complete:
        return publish_order(db, order_id, strict=is_strict)
    if wants_publish and not plan.complete:
        db.add_order_event(order_id, "publish_blocked", json.dumps({"reason": "missing_pairs"}), now)

    row = db.get_order(order_id)
    data = serialize_order(row, db)
    if is_strict and not plan.complete:
        raise ApiError(422, "order cannot be published: some images have no complete calibration pair",
                       order=data, missing_images=snapshot["missing_images"])
    return data


def _load_images(row: Any) -> list[ImageSpec]:
    raw_list = json.loads(row["images_json"])
    specs = []
    for n in raw_list:
        specs.append(
            ImageSpec(
                image_id=n["image_id"],
                detector=n.get("detector", ""),
                exposure_key=n.get("exposure_key") or exposure_key(n.get("exposure_seconds")),
                filter_name=n.get("filter"),
                exposure_seconds=n.get("exposure_seconds"),
                raw=n.get("raw") or n,
            )
        )
    return specs


def publish_order(db: Database, identifier: Any, strict: bool = False) -> dict[str, Any]:
    with db.lock:
        row = db.get_order(identifier)
        if row is None:
            raise ApiError(404, f"order {identifier!r} not found", order_id=identifier)
        order_id = row["id"]

        # Already issued manifests are immutable ("the manifest issued then").
        if row["status"] == "published" and row["issued_manifest_json"]:
            data = serialize_order(row, db)
            data["already_published"] = True
            return data

        pk_ids = json.loads(row["candidate_ids_json"])
        cand_rows = {r["id"]: r for r in (db.get_file_by_pk(pk) for pk in pk_ids) if r is not None}
        candidates = [CalibFile.from_row(r) for r in cand_rows.values()]

        old_snapshot = json.loads(row["plan_json"]) if row["plan_json"] else None
        old_selected = (old_snapshot or {}).get("selected_pk_ids", [])
        deactivated = [
            pk for pk in old_selected
            if pk not in cand_rows or not bool(cand_rows[pk]["is_active"])
        ]

        must_recompute = (
            old_snapshot is None
            or not old_snapshot.get("complete")
            or bool(deactivated)
            or row["status"] != "ready"
        )

        now = utcnow()
        if must_recompute:
            specs = _load_images(row)
            plan = build_plan(specs, candidates)
            snapshot = make_plan_snapshot(plan, now)
            new_status = "ready" if plan.complete else "unmatched"
            db.update_computation(
                order_id, new_status,
                json.dumps(snapshot, ensure_ascii=False, default=str),
                plan.total_bytes,
                json.dumps(snapshot["missing_image_details"], ensure_ascii=False, default=str),
                now,
            )
            db.add_order_event(
                order_id,
                "recomputed_before_publish" if deactivated else "computed_at_publish",
                json.dumps({"deactivated_pk_ids": deactivated, "complete": plan.complete}, default=str),
                now,
            )
            if not plan.complete:
                row = db.get_order(order_id)
                data = serialize_order(row, db)
                data["publish_blocked"] = True
                data["published"] = False
                if strict:
                    raise ApiError(422, "order cannot be published: missing calibration pairs",
                                   order=data, missing_images=snapshot["missing_images"])
                return data
            recomputed = bool(deactivated) or (old_snapshot is not None and not old_snapshot.get("complete"))
        else:
            snapshot = old_snapshot
            recomputed = False

        order_no = row["order_no"]
        manifest = make_manifest(order_id, order_no, snapshot, now, recomputed, deactivated)
        manifest_json = json.dumps(manifest, ensure_ascii=False, default=str)
        db.publish_order(order_id, manifest_json, snapshot["total_bytes"], now, recomputed)
        db.add_order_event(
            order_id, "published",
            json.dumps({"total_bytes": snapshot["total_bytes"], "recomputed": recomputed}, default=str),
            now,
        )
        row = db.get_order(order_id)
        return serialize_order(row, db)


def recompute_order(db: Database, identifier: Any) -> dict[str, Any]:
    """Explicitly recompute the draft. Published orders stay immutable."""
    with db.lock:
        row = db.get_order(identifier)
        if row is None:
            raise ApiError(404, f"order {identifier!r} not found")
        if row["status"] == "published" and row["issued_manifest_json"]:
            data = serialize_order(row, db)
            data["already_published"] = True
            data["note"] = "published manifests are immutable; returning the issued manifest"
            return data
        specs = _load_images(row)
        pk_ids = json.loads(row["candidate_ids_json"])
        candidates = []
        for pk in pk_ids:
            cand_row = db.get_file_by_pk(pk)
            if cand_row is not None:
                candidates.append(CalibFile.from_row(cand_row))
        now = utcnow()
        plan = build_plan(specs, candidates)
        snapshot = make_plan_snapshot(plan, now)
        db.update_computation(
            row["id"], "ready" if plan.complete else "unmatched",
            json.dumps(snapshot, ensure_ascii=False, default=str),
            plan.total_bytes,
            json.dumps(snapshot["missing_image_details"], ensure_ascii=False, default=str),
            now,
        )
        db.add_order_event(row["id"], "recomputed", None, now)
        return serialize_order(db.get_order(row["id"]), db)


def _candidates_for_order(db: Database, row: Any) -> list[CalibFile]:
    pk_ids = json.loads(row["candidate_ids_json"])
    candidates = []
    for pk in pk_ids:
        cand_row = db.get_file_by_pk(pk)
        if cand_row is not None:
            candidates.append(CalibFile.from_row(cand_row))
    return candidates


def preview_order(db: Database, row: Any) -> dict[str, Any]:
    """GET view of an unpublished order.

    Drafts are recomputed against the currently active candidate set so a
    caller inspecting an order after a deactivation sees the plan that a
    subsequent publish would freeze.  Nothing is persisted here; publish keeps
    the authoritative check -> recompute -> freeze critical section.
    Published orders always return the frozen issued manifest.
    """
    if row["status"] == "published" and row["issued_manifest_json"]:
        return serialize_order(row)

    stored = json.loads(row["plan_json"]) if row["plan_json"] else None
    specs = _load_images(row)
    plan = build_plan(specs, _candidates_for_order(db, row))
    fresh = make_plan_snapshot(plan, utcnow())

    old_ids = set((stored or {}).get("selected_pk_ids", []))
    new_ids = set(fresh["selected_pk_ids"])
    old_complete = bool((stored or {}).get("complete"))
    stale = old_ids != new_ids or old_complete != plan.complete
    data = serialize_order(row, fresh_snapshot=fresh)
    data["draft_stale"] = stale
    data["refreshed"] = True
    return data


def serialize_order(row: Any, db: Database | None = None,
                    fresh_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    d = dict(row)
    order_id = d["id"]
    published = d["status"] == "published" and bool(d.get("issued_manifest_json"))

    candidate_pks = json.loads(d.get("candidate_ids_json") or "[]")
    images = json.loads(d.get("images_json") or "[]")

    data: dict[str, Any] = {
        "id": order_id,
        "order_id": order_id,
        "orderId": order_id,
        "order_no": d.get("order_no"),
        "orderNo": d.get("order_no"),
        "order_number": d.get("order_no"),
        "status": d.get("status"),
        "published": bool(published),
        "created_at": d.get("created_at"),
        "computed_at": d.get("computed_at"),
        "published_at": d.get("published_at"),
        "issued_at": d.get("published_at"),
        "images": images,
        "image_count": len(images),
        "candidate_file_ids": candidate_pks,
        "candidate_ids": candidate_pks,
        "candidateIds": candidate_pks,
        "max_candidates": MAX_CANDIDATES,
    }

    if published:
        manifest = json.loads(d["issued_manifest_json"])
        # Top-level fields ARE the issued manifest (frozen snapshot).
        data.update(manifest)
        data["manifest"] = manifest
        data["issued_manifest"] = manifest
        data["result"] = manifest
        data["data"] = manifest
        data["events_href"] = f"/api/orders/{order_id}/events"
        return data

    snapshot = fresh_snapshot if fresh_snapshot is not None else (
        json.loads(d["plan_json"]) if d.get("plan_json") else None
    )
    if fresh_snapshot is not None:
        missing_details = fresh_snapshot.get("missing_image_details", [])
    else:
        missing_details = json.loads(d.get("missing_json") or "[]")
    missing_ids = [m.get("image_id") if isinstance(m, dict) else m for m in missing_details]
    if snapshot:
        items = snapshot.get("items", [])
        files = snapshot.get("files", [])
        total = snapshot.get("total_bytes")
        if fresh_snapshot is not None:
            missing_ids = snapshot.get("missing_image_ids", missing_ids)
    else:
        items, files, total = [], [], None
        if not missing_ids:
            missing_ids = snapshot.get("missing_image_ids", []) if snapshot else []

    complete = bool(snapshot and snapshot.get("complete"))
    # An incomplete order has no valid whole-order download total; keep any
    # best-effort partial optimum separately under ``partial_total_bytes``.
    order_total = total if complete else None
    draft_manifest = {
        "manifest_no": f"M-{order_id:06d}",
        "order_id": order_id,
        "orderId": order_id,
        "id": order_id,
        "order_no": d.get("order_no"),
        "orderNo": d.get("order_no"),
        "status": d.get("status"),
        "published": False,
        "draft": True,
        "issued_at": None,
        "published_at": None,
        "computed_at": (snapshot or {}).get("computed_at") if snapshot else d.get("computed_at"),
        "image_count": len(images),
        "items": items,
        "pairings": items,
        "pairs": items,
        "matches": items,
        "assignments": items,
        "files": files if complete else [],
        "unique_files": files if complete else [],
        "total_bytes": order_total,
        "totalBytes": order_total,
        "total_download_bytes": order_total,
        "download_bytes": order_total,
        "partial_total_bytes": total,
        "missing_images": missing_ids,
        "missing_image_ids": missing_ids,
        "missing_image_details": missing_details,
        "publishable": complete,
    }
    data.update({
        "publishable": complete,
        "can_publish": complete,
        "ok": complete,
        "manifest": draft_manifest,
        "draft_manifest": draft_manifest,
        "issued_manifest": None,
        "result": draft_manifest,
        "data": draft_manifest,
        "draft": snapshot,
        "plan": snapshot,
        "items": items,
        "pairings": items,
        "pairs": items,
        "matches": items,
        "assignments": items,
        "files": files if complete else [],
        "unique_files": files if complete else [],
        "total_bytes": order_total,
        "totalBytes": order_total,
        "total_download_bytes": order_total,
        "download_bytes": order_total,
        "partial_total_bytes": total,
        "missing_images": missing_ids,
        "missing_image_ids": missing_ids,
        "missing_image_details": missing_details,
        "missing": missing_ids,
        "recomputed_at_publish": bool(d.get("recomputed_at_publish")),
    })
    return data
