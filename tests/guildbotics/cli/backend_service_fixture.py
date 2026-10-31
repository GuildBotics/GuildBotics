"""Real API/backend process, with deterministic service work and no providers."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from guildbotics.app_api import lifecycle, server


class Worker:
    def __init__(self, *args, **kwargs):
        self.done = threading.Event()

    def start(self):
        if os.environ.get("SERVICE_TEST_MODE") != "self_stop":
            self.done.wait()

    def shutdown(self, *, graceful=True, timeout=None):
        if graceful and os.environ.get("SERVICE_TEST_MODE") == "hold":
            Path("graceful").touch()
            assert self.done.wait(15)
        else:
            self.done.set()

    def get_status_summary(self):
        return {}


def main():
    original = server.create_app

    def create_app(**kwargs):
        app = original(**kwargs)
        runtime = app.state.runtime

        scheduler = runtime._lifecycle._scheduler
        start = scheduler.start

        def start_or_fail(**kwargs):
            if os.environ.get("SERVICE_TEST_MODE") == "start_failure":
                raise RuntimeError("scheduler start failed")
            return start(**kwargs)

        scheduler.start = start_or_fail
        scheduler._context_factory = lambda: None
        return app

    lifecycle.TaskScheduler = Worker
    server.create_app = create_app
    server.main()


if __name__ == "__main__":
    main()
