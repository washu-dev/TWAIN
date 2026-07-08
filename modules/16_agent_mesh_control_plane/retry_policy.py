
import time as _time
from random import Random
from enum import Enum
from collections import deque


class ErrorType(Enum):
    PERMANENT = "PERMANENT"
    TRANSIENT = "TRANSIENT"


class PermanentError(Exception):
    pass


class TransientError(Exception):
    pass


def classify_error(error):
    if isinstance(error, PermanentError):
        return ErrorType.PERMANENT
    if isinstance(error, TransientError):
        return ErrorType.TRANSIENT
    if isinstance(error, (ConnectionError, OSError, TimeoutError)):
        return ErrorType.TRANSIENT

    status = getattr(error, "status", None)
    if status is None:
        resp = getattr(error, "response", None)
        status = getattr(resp, "status_code", None)
    if status is not None:
        if status in (408, 429, 500, 502, 503, 504):
            return ErrorType.TRANSIENT

    return ErrorType.PERMANENT


class RetryPolicy:
    def __init__(self, max_retries=3, base_delay=1.0, jitter=0.1,
                 sleep=_time.sleep, rng=None):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.jitter = jitter
        self._sleep = sleep
        self._rng = rng or Random()

    def delay_for(self, attempt):
        return self.base_delay * (2 ** attempt) * (
            1 + self._rng.uniform(-self.jitter, self.jitter)
        )

    def execute(self, func, *args, **kwargs):
        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_exc = e
                if classify_error(e) == ErrorType.PERMANENT:
                    raise
                if attempt < self.max_retries:
                    self._sleep(max(0.0, self.delay_for(attempt)))
        raise last_exc


class CircuitBreakerStates(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class AlreadyOpenError(Exception):
    pass


class CircuitBreaker:
    def __init__(self, max_errors=5, error_window=60, cooldown=300,
                 clock=_time.time):
        self.max_errors = max_errors
        self.error_window = error_window
        self.cooldown = cooldown
        self.state = CircuitBreakerStates.CLOSED
        self.failures = deque()
        self.opened_timestamp = None
        self._clock = clock

    def _maybe_transition(self):
        if self.state == CircuitBreakerStates.OPEN and self.opened_timestamp is not None:
            if self._clock() - self.opened_timestamp > self.cooldown:
                self.state = CircuitBreakerStates.HALF_OPEN

    def _open(self):
        self.state = CircuitBreakerStates.OPEN
        self.opened_timestamp = self._clock()

    def _close(self):
        self.state = CircuitBreakerStates.CLOSED
        self.opened_timestamp = None
        self.failures.clear()

    def record_success(self):
        self._close()

    def record_failure(self):
        now = self._clock()
        self.failures.append(now)
        while self.failures and now - self.failures[0] > self.error_window:
            self.failures.popleft()
        if len(self.failures) >= self.max_errors:
            self._open()

    def execute(self, func, *args, **kwargs):
        self._maybe_transition()
        if self.state == CircuitBreakerStates.OPEN:
            raise AlreadyOpenError(
                f"Circuit breaker is open after {self.max_errors} failures "
                f"within {self.error_window}s; cooling down for {self.cooldown}s"
            )
        try:
            result = func(*args, **kwargs)
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise


class ResilientCaller:
    def __init__(self, max_retries=3, base_delay=1.0, jitter=0.1,
                 max_errors=5, error_window=60, cooldown=300,
                 sleep=_time.sleep, rng=None, clock=_time.time):
        self.circuit_breaker = CircuitBreaker(
            max_errors=max_errors,
            error_window=error_window,
            cooldown=cooldown,
            clock=clock,
        )
        self.retry_policy = RetryPolicy(
            max_retries=max_retries,
            base_delay=base_delay,
            jitter=jitter,
            sleep=sleep,
            rng=rng,
        )

    def execute(self, func, *args, **kwargs):
        def retried():
            return self.retry_policy.execute(func, *args, **kwargs)
        return self.circuit_breaker.execute(retried)

