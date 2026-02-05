"""Reusable launch status polling helper."""

from __future__ import annotations

import time


def wait_for_launch(
    launcher: str | None = None,
    batch_id: str | None = None,
    timeout: int = 30,
) -> dict:
    """Block until launch batch is ready, times out, or errors.

    Args:
        launcher: Instance name of the launcher (for aggregated lookup).
        batch_id: Specific batch ID (takes priority over launcher).
        timeout: Max seconds to wait.

    Returns:
        Dict with keys:
            status: "ready" | "timeout" | "error" | "no_launches"
            expected, ready, instances, launcher, timestamp (when available)
            hint (on timeout/error)
    """
    from .db import get_launch_status, get_launch_batch
    from .instances import cleanup_stale_placeholders

    cleanup_stale_placeholders()

    def _fetch() -> dict | None:
        if batch_id:
            return get_launch_batch(batch_id)
        return get_launch_status(launcher)

    status_data = _fetch()
    if not status_data:
        msg = "You haven't launched any instances" if launcher else "No launches found"
        return {"status": "no_launches", "message": msg}

    # Poll until ready or timeout
    start = time.time()
    while status_data["ready"] < status_data["expected"] and time.time() - start < timeout:
        time.sleep(0.5)
        status_data = _fetch()
        if not status_data:
            return {
                "status": "error",
                "message": "Launch data disappeared (DB reset or pruned)",
            }

    # Build result
    is_timeout = status_data["ready"] < status_data["expected"]
    result = {
        "status": "timeout" if is_timeout else "ready",
        "expected": status_data["expected"],
        "ready": status_data["ready"],
        "instances": status_data["instances"],
        "launcher": status_data["launcher"],
        "timestamp": status_data["timestamp"],
    }

    if "batches" in status_data:
        result["batches"] = status_data["batches"]
    else:
        result["batch_id"] = status_data.get("batch_id")

    if is_timeout:
        result["timed_out"] = True
        batch_info = result.get("batch_id") or (
            result.get("batches", ["?"])[0] if result.get("batches") else "?"
        )
        result["hint"] = (
            f"Launch failed: {status_data['ready']}/{status_data['expected']} ready after {timeout}s "
            f"(batch: {batch_info}). Check ~/.hcom/.tmp/logs/background_*.log or hcom list -v"
        )

    return result
