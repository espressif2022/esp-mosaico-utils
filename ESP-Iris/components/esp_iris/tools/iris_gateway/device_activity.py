"""Cooperative shutdown of device work, shared by handoff and ROM operations."""
from __future__ import annotations

import asyncio
from typing import Any


async def stop_session_work(service: Any, device_id: str, timeout: float) -> None:
    """Require protocol completion; never clear activity to manufacture idleness."""
    hub = service.device_hub
    deadline = asyncio.get_running_loop().time() + timeout

    def remaining() -> float:
        value = deadline - asyncio.get_running_loop().time()
        if value <= 0:
            raise TimeoutError("mirrors or background jobs did not finish cancellation")
        return value

    for channel in hub.active_mirrors(device_id):
        wait_timeout = remaining()
        await asyncio.wait_for(hub.mirror_stop(device_id, channel), wait_timeout)
    requested = set()
    while True:
        jobs = [key[1] for key in service.jobs if key[0] == device_id]
        if not jobs:
            return
        for job_id in jobs:
            wait_timeout = remaining()
            result = await asyncio.wait_for(
                hub.job(device_id, job_id, cancel=job_id not in requested), wait_timeout)
            requested.add(job_id)
            service.observe_device_activity({"kind": "job", "device_id": device_id, **result})
        if any(key[0] == device_id for key in service.jobs):
            await asyncio.sleep(min(0.05, remaining()))
