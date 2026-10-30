"""Bounded web workers, memory admission and shared-model synchronization."""

from contextlib import contextmanager
from functools import wraps
import ctypes
import os
import threading
import time

GIB = 1024**3


class SystemResources:
    def __init__(self):
        self.previous = None
        self.cpu = None

    def __call__(self):
        if os.name != "nt":
            return dict(available=None, total=None, cpu=None)
        from ctypes import wintypes

        class Memory(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
                (name, ctypes.c_ulonglong)
                for name in (
                    "total",
                    "available",
                    "page",
                    "free_page",
                    "virtual",
                    "free_virtual",
                    "extended",
                )
            ]

        memory = Memory()
        memory.length = ctypes.sizeof(memory)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            return dict(available=None, total=None, cpu=None)
        values = [wintypes.FILETIME() for _ in range(3)]
        now = time.monotonic()
        if ctypes.windll.kernel32.GetSystemTimes(*(ctypes.byref(v) for v in values)):
            idle, kernel, user = [
                v.dwHighDateTime * 2**32 + v.dwLowDateTime for v in values
            ]
            total = kernel + user
            if self.previous and now - self.previous[0] >= 0.5:
                _, previous_idle, previous_total = self.previous
                delta = total - previous_total
                if delta > 0:
                    self.cpu = max(
                        0.0, min(100.0, 100 * (1 - (idle - previous_idle) / delta))
                    )
            if self.previous is None or now - self.previous[0] >= 0.5:
                self.previous = (now, idle, total)
        return dict(available=memory.available, total=memory.total, cpu=self.cpu)


class Capacity:
    """Two routes at most; OCR/record confirmation occupies an exclusive slot."""

    def __init__(self, probe=None, model_ready=None, wait_seconds=120):
        self.probe = probe or SystemResources()
        self.model_ready = model_ready or (lambda: True)
        self.wait_seconds = wait_seconds
        self.condition = threading.Condition()
        self.routes = 0
        self.exclusive = False
        self.cold_start = False
        self.closed = False

    def _allowed(self, kind):
        is_route = kind in {"route", "transit"}
        if self.exclusive or (not is_route and self.routes):
            return False
        state = self.probe()
        free, total = state["available"], state["total"]
        ready = self.model_ready() or kind == "transit"
        # Unknown resource counters use one worker rather than guessing capacity.
        if free is None or total is None:
            return self.routes == 0
        minimum = 3 * GIB if ready else 6 * GIB
        if free < minimum:
            return False
        if self.routes == 0:
            return True
        return (
            is_route
            and self.routes < 2
            and ready
            and not (self.cold_start and not self.model_ready())
            and (os.cpu_count() or 1) >= 4
            and total >= 16 * GIB
            and free >= 6 * GIB
            and (state["cpu"] is None or state["cpu"] < 85)
        )

    @contextmanager
    def slot(self, kind):
        deadline = time.monotonic() + self.wait_seconds
        with self.condition:
            while not self.closed and not self._allowed(kind):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError(
                        "PC의 여유 자원이 부족합니다. 다른 작업을 줄인 뒤 다시 시도해 주세요."
                    )
                self.condition.wait(min(1.0, remaining))
            if self.closed:
                raise ValueError("서버가 종료 중입니다. 잠시 후 다시 접속해 주세요.")
            if kind in {"route", "transit"}:
                self.routes += 1
                if kind == "route" and not self.model_ready():
                    self.cold_start = True
            else:
                self.exclusive = True
        try:
            yield
        finally:
            with self.condition:
                if kind in {"route", "transit"}:
                    self.routes -= 1
                    if self.routes == 0:
                        self.cold_start = False
                else:
                    self.exclusive = False
                self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class LockedModel:
    """Share immutable artifacts; serialize inference and its mutable caches."""

    def __init__(self, model, lock):
        self._model = model
        self._lock = lock

    def __getattr__(self, name):
        value = getattr(self._model, name)
        if name not in {
            "predict",
            "predict_batch",
            "predict_with_kma",
            "predict_with_kma_batch",
        }:
            return value

        @wraps(value)
        def invoke(*args, **kwargs):
            with self._lock:
                return value(*args, **kwargs)

        return invoke


def configure_process():
    """Configure the server process before NumPy/Torch/OCR imports."""
    for name in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ[name] = "2"
    if os.name == "nt":
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        # Apply only to this server. Windows schedules foreground apps first.
        kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000)
