"""Background jobs, so pages never freeze while AI calls run.

A check (or a guideline import) is saved first with state 'checking' / 'reading' and then run here. The page
the user lands on shows progress and refreshes itself. If the server restarts mid-job, `recover()` picks the
unfinished jobs up again on the next start.
"""
from __future__ import annotations

import contextvars
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("brandproof.jobs")


class Jobs:
    def __init__(self, workers: int = 3):
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bp-job")
        self.running: set[str] = set()
        self.lock = threading.Lock()

    def submit(self, key: str, fn, *args) -> bool:
        """Run fn(*args) in the background as the current user. Returns False if the same job is already running."""
        with self.lock:
            if key in self.running:
                return False
            self.running.add(key)
        ctx = contextvars.copy_context()
        self.pool.submit(self._run, key, ctx, fn, args)
        return True

    def _run(self, key, ctx, fn, args):
        try:
            ctx.run(fn, *args)
        except Exception:
            log.exception("background job %s failed", key)
        finally:
            with self.lock:
                self.running.discard(key)

    def is_running(self, key: str) -> bool:
        with self.lock:
            return key in self.running

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """For tests and shutdown: wait until no job is running."""
        import time
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if not self.running:
                    return True
            time.sleep(0.05)
        return False
