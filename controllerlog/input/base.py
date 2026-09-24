"""Input backend interface."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod

from ..hub import Hub


class Backend(ABC):
    """A source of controller input that publishes into a :class:`Hub`.

    Lifecycle: ``start()`` spawns a daemon thread running :meth:`run` until
    :meth:`stop` is called. Implementations must publish timestamps in the
    ``time.perf_counter_ns()`` domain (see :mod:`controllerlog.hub`).
    """

    name = "backend"

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: BaseException | None = None
        self.ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_guarded, name=f"{self.name}-input",
                                        daemon=True)
        self._thread.start()

    def _run_guarded(self) -> None:
        try:
            self.run()
        except BaseException as e:  # surfaced to the CLI via .error, no thread traceback spam
            self.error = e
            import logging
            logging.getLogger(__name__).debug("%s backend stopped", self.name, exc_info=True)
        finally:
            self.ready.set()  # never leave ready.wait() callers hanging (error is set first)

    @abstractmethod
    def run(self) -> None:
        """Blocking loop; must return soon after ``self._stop`` is set."""

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()
