"""Nonblocking presentation snapshots; never use these for physical decisions.

``fetch`` must perform a fresh read. Only the one background worker calls it.
The clocks in snapshots are monotonic and remain unchanged on cache reads;
``age`` is the age of the last successful snapshot, not the last attempt.
Transport calls must have their own timeout: close cannot interrupt HTTP, but
it prevents new work and discards late results without waiting for the network.
"""
from copy import deepcopy
import math
import re
import threading
import time


class StatusCache:
    def __init__(self, fetch, *, ttl=2.0, retry_interval=5.0,
                 clock=time.monotonic, stopping=lambda: False):
        self._ttl, self._retry_interval = float(ttl), float(retry_interval)
        if (not math.isfinite(self._ttl) or self._ttl < 0 or
                not math.isfinite(self._retry_interval) or self._retry_interval <= 0):
            raise ValueError("invalid_status_cache_interval")
        self._fetch, self._clock, self._stopping = fetch, clock, stopping
        self._lock = threading.Lock()
        self._wakeup = threading.Event()
        self._thread = None
        self._closed = False
        self._refreshing = False
        self._context = self._error = None
        self._checked_at = self._success_at = self._next_refresh = None
        self._generation = 0

    def read(self):
        """Return an isolated snapshot immediately; request an update if due."""
        with self._lock:
            now = self._clock()
            self._request_locked(now)
            age = None if self._success_at is None else max(0, now - self._success_at)
            if self._context is None:
                state = "offline" if self._error is not None else "checking"
            else:
                state = "fresh" if self._error is None and age < self._ttl else "stale"
            return {"context": deepcopy(self._context), "error": self._error,
                    "checked_at": self._checked_at, "success_at": self._success_at,
                    "age": age, "state": state, "generation": self._generation,
                    "refreshing": self._refreshing and not self._stopped()}

    def request(self):
        """Schedule at most one refresh, respecting freshness and error backoff."""
        with self._lock:
            return self._request_locked(self._clock())

    def _stopped(self):
        return self._closed or self._stopping()

    def _request_locked(self, now):
        if (self._stopped() or self._refreshing or
                self._next_refresh is not None and now < self._next_refresh):
            return False
        if self._thread is None:
            self._thread = threading.Thread(target=self._work, name="status-cache", daemon=True)
            try:
                self._thread.start()
            except RuntimeError:
                self._thread = None
                self._error, self._checked_at = "status_unavailable", now
                self._next_refresh = now + self._retry_interval
                self._generation += 1
                return False
        self._refreshing = True
        self._wakeup.set()
        return True

    def _work(self):
        while True:
            self._wakeup.wait()
            self._wakeup.clear()
            with self._lock:
                if self._stopped():
                    self._refreshing = False
                    return
            context, error = None, None
            try:
                context = self._fetch()
                if not isinstance(context, dict):
                    raise ValueError("invalid_status_context")
                context = deepcopy(context)
            except Exception as exc:
                code = getattr(exc, "code", None)
                error = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "status_unavailable"
            with self._lock:
                self._refreshing = False
                if self._stopped():
                    return
                now = self._clock()
                self._checked_at, self._error = now, error
                self._generation += 1
                if error is None:
                    self._context, self._success_at = context, now
                self._next_refresh = now + (self._retry_interval if error is not None else self._ttl)

    def close(self, timeout=0):
        """Stop accepting work; optionally join for a bounded number of seconds."""
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("invalid_status_cache_close_timeout")
        with self._lock:
            self._closed = True
            self._refreshing = False
            self._wakeup.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
