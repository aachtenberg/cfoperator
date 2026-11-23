"""Put a deadline on a call that can hang, without stacking up callers.

A health call that queries something over the network has two ways to fail.
It can raise, which is quick and easy to report. Or it can sit there: a query
on a pooled connection whose link has dropped waits for TCP to give up, which
is minutes. A caller that has to answer inside a probe timeout cannot wait for
that, and it cannot interrupt it either, since Python has no way to cancel a
thread blocked in a socket read.

So the call runs on a thread of its own and the caller waits a bounded time for
it. The part that matters is what the *next* caller does. A probe arrives every
few seconds; if each one started its own thread, a ten minute outage would park
sixty of them, which is the pile-up this replaces (CFOP-768). A caller that
arrives while a call is still out waits on that same call instead.
"""

from __future__ import annotations

import threading
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class DeadlineExceeded(Exception):
    """The call did not answer in time. It is still running."""


class _Flight(Generic[T]):
    __slots__ = ("done", "result", "error", "reported")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: T | None = None
        self.error: BaseException | None = None
        self.reported = False


class SingleFlight(Generic[T]):
    """One call to ``fn`` in flight at a time, shared by everyone waiting."""

    def __init__(self, fn: Callable[[], T], name: str = "single-flight"):
        self._fn = fn
        self._name = name
        self._lock = threading.Lock()
        self._flight: _Flight[T] | None = None

    def call(self, timeout: float) -> T:
        """Return ``fn()``, or raise ``DeadlineExceeded`` after ``timeout`` seconds.

        Whatever ``fn`` raises is raised here, for every caller sharing the flight.
        """
        flight = self._join_or_start()
        if not flight.done.wait(max(0.0, timeout)):
            raise DeadlineExceeded(self._name)
        if flight.error is not None:
            raise flight.error
        return flight.result  # type: ignore[return-value]

    def first_report(self) -> bool:
        """True once per stuck flight, so a caller can log a hang one time."""
        with self._lock:
            flight = self._flight
            if flight is None or flight.done.is_set() or flight.reported:
                return False
            flight.reported = True
            return True

    def _join_or_start(self) -> _Flight[T]:
        with self._lock:
            flight = self._flight
            if flight is not None and not flight.done.is_set():
                return flight
            flight = _Flight()
            self._flight = flight
        # Daemon: a call that never returns must not hold the process open at
        # shutdown, which is exactly when a dead dependency is likeliest.
        threading.Thread(target=self._run, args=(flight,), daemon=True, name=self._name).start()
        return flight

    def _run(self, flight: _Flight[T]) -> None:
        try:
            flight.result = self._fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the caller, not swallowed
            flight.error = exc
        finally:
            flight.done.set()
