from __future__ import annotations

import hashlib
import json
from typing import Any


def investment_flow_signature(snapshots: list[dict[str, Any]]) -> str:
    """Fingerprint the dates and cumulative contributions used by benchmarks."""
    flows = [(snapshot["date"], float(snapshot["invested"])) for snapshot in snapshots]
    payload = json.dumps(flows, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(f"benchmark-flow-v1:{payload}".encode("utf-8")).hexdigest()


def benchmark_cache_matches(
    cached: list[dict[str, Any]] | None,
    snapshots: list[dict[str, Any]],
    required_tickers: list[str],
    flow_signature: str,
) -> bool:
    """Reject persisted benchmark values from a different date or cash-flow series."""
    if not cached or len(cached) != len(snapshots):
        return False
    if [entry.get("date") for entry in cached] != [snapshot["date"] for snapshot in snapshots]:
        return False
    if cached[0].get("_flow_signature") != flow_signature:
        return False
    return all(ticker in cached[0] for ticker in required_tickers)