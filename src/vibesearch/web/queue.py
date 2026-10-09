"""A request-independent worker keeps cancelled inference physically serialized."""
import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Callable, Any


class QueueBusy(Exception):
    pass


@dataclass
class _Job:
    operation: Callable[[], Any]
    result: asyncio.Future
    started: asyncio.Event


class InferenceQueue:
    def __init__(self, capacity=8, timeout=30):
        self.capacity, self.timeout = capacity, timeout
        self.waiting = deque()
        self.wakeup = asyncio.Event()
        self.closing = False
        self.worker = None

    async def __aenter__(self):
        self.worker = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *_):
        self.closing = True
        self.wakeup.set()
        assert self.worker is not None
        cancelled = False
        while not self.worker.done():
            try:
                await asyncio.shield(self.worker)
            except asyncio.CancelledError:
                cancelled = True
        # Retrieve worker failures before releasing the app's process lock.
        self.worker.result()
        if cancelled:
            raise asyncio.CancelledError

    async def submit(self, operation):
        if self.closing or len(self.waiting) >= self.capacity:
            raise QueueBusy('Inference queue is full; retry later')
        job = _Job(operation, asyncio.get_running_loop().create_future(), asyncio.Event())
        self.waiting.append(job)
        self.wakeup.set()
        try:
            try:
                await asyncio.wait_for(job.started.wait(), self.timeout)
            except TimeoutError:
                raise QueueBusy('Inference admission timed out; retry later') from None
            return await asyncio.shield(job.result)
        except BaseException:
            if job in self.waiting:
                self.waiting.remove(job)
            job.result.cancel()
            raise

    async def _run(self):
        while True:
            if not self.waiting:
                if self.closing:
                    return
                self.wakeup.clear()
                await self.wakeup.wait()
                continue
            job = self.waiting.popleft()
            job.started.set()
            try:
                value = await asyncio.to_thread(job.operation)
            except Exception as exc:
                if not job.result.done():
                    job.result.set_exception(exc)
            else:
                if not job.result.done():
                    job.result.set_result(value)
