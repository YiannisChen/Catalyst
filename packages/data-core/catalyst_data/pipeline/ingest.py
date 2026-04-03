from __future__ import annotations

import time

from catalyst_data.pipeline.stages import StageResult


def run_ingest(
    endpoint_data: dict,
    endpoint_statuses: dict,
) -> StageResult:
    """Validate raw fetch results, keeping only successful endpoints.

    Returns ``StageResult(ok=True)`` when at least one endpoint delivered data,
    or ``StageResult(ok=False)`` when every endpoint failed.
    """
    t0 = time.perf_counter()

    successful: dict = {}
    for key, payload in endpoint_data.items():
        status = endpoint_statuses.get(key, 0)
        if status == 200 and payload is not None:
            successful[key] = payload

    elapsed = (time.perf_counter() - t0) * 1000

    if not successful:
        return StageResult(
            stage="ingest",
            ok=False,
            error="No endpoint returned data",
            latency_ms=elapsed,
        )

    return StageResult(
        stage="ingest",
        ok=True,
        data=successful,
        latency_ms=elapsed,
    )
