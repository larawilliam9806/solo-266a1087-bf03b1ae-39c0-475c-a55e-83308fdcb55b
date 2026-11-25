#!/usr/bin/env python3
"""Seed a demo calibration registry against a running server.

Usage:
    python scripts/seed_demo.py [base_url]

Creates two calibration batches (B1 expensive, B2 cheap) and prints an order
payload that demonstrates reuse / minimum download.
"""

import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    ids = []
    specs = [
        {"kind": "dark", "exposure_seconds": 60, "batch_id": "B1", "size_bytes": 100_000_000},
        {"kind": "flat", "filter": "r", "batch_id": "B1", "size_bytes": 100_000_000},
        {"kind": "dark", "exposure_seconds": 60, "batch_id": "B2", "size_bytes": 4096},
        {"kind": "flat", "filter": "r", "batch_id": "B2", "size_bytes": 5120},
    ]
    for spec in specs:
        spec.update({"detector": "CCD-A"})
        created = post("/api/calibration-files", spec)
        ids.append(created["id"])
        print("registered", created["id"], created["kind"], created.get("batch_id"))

    order = {
        "images": [
            {"image_id": "M42-001", "detector": "CCD-A", "exposure_seconds": 60, "filter": "r"},
            {"image_id": "M42-002", "detector": "CCD-A", "exposure_seconds": 60, "filter": "r"},
        ],
        "candidate_file_ids": ids,
    }
    result = post("/api/orders", order)
    print("draft order:")
    print(json.dumps({
        "id": result["id"],
        "order_no": result["order_no"],
        "status": result["status"],
        "total_bytes": result["total_bytes"],
        "selected": result["candidate_file_ids"] and [f["id"] for f in result["files"]],
    }, ensure_ascii=False, indent=2))
    print("Publish with:")
    print(f"  curl -X POST {BASE}/api/orders/{result['id']}/publish")


if __name__ == "__main__":
    main()
