
from random import random
from enum import Enum
from time import time
from collections import deque

class ErrorType(Enum):
    PERMANENT = "PERMANENT"
    TRANSIENT = "TRANSIENT"

class PermanentError(Exception):
    pass

class TransientError(Exception):
    pass

def classify_error(error):
    if(isinstance(error, PermanentError)):
        return ErrorType.PERMANENT
    if(isinstance(error, TransientError)):
        return ErrorType.TRANSIENT
    if(isinstance(error, (ConnectionError,OSError, TimeoutError))):
        return ErrorType.TRANSIENT

    status = getattr(error, "status", None)
    if status is not None:
        if status in (408,429,500,502,503,504):
            return ErrorType.TRANSIENT

    return ErrorType.PERMANENT

class RetryPolicy:
    def __init__(self, max_retries=3, jitter=0.1):
        self.retries = 0
        self.max_retries = max_retries
        self.base_value = 1
        self.jitter = jitter

    @property
    def delay(self):
        return (self.base_value * 2**self.retries) * (1+random.uniform(-self.jitter, self.jitter))

    def execute(self, func, *args, **kwargs):
        self.retries = 0
        while self.retries <= self.max_retries:
            self.retries += 1
            try:
                result = func(*args, **kwargs)
            except Exception as e:
                error_type = classify_error(e)
                if error_type == ErrorType.PERMANENT:
                    raise
                elif error_type == ErrorType.TRANSIENT:
                    time.sleep(self.delay)
            raise


class CircuitBreakerStates(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

class AlreadyOpenError(Exception):
    pass

class CircuitBreaker:
    def __init__(self, max_errors=5,error_window=60,cooldown=300):
        self.max_errors = max_errors
        self.error_window = error_window
        self.cooldown = cooldown
        self.state = CircuitBreakerStates.CLOSED
        self.failures = deque()
        self.opened_timestamp = float | None

    def transition(self):
        if (self.state == CircuitBreakerStates.OPEN):
            if(self.opened_timestamp == None): self.opened_timestamp = time.time()
            if(time.time() - self.opened_timestamp > self.cooldown):
                self.state = CircuitBreakerStates.HALF_OPEN

    def open(self):
        self.state = CircuitBreakerStates.OPEN
        self.opened_timestamp = time.time()
    def close(self):
        self.state = CircuitBreakerStates.CLOSED
        self.opened_timestamp = None
    def half_open(self):
        self.state = CircuitBreakerStates.HALF_OPEN

    def success(self):
        self.state = self.close()
        self.opened_timestamp = None
        self.failures = deque()
    def failure(self):
        self.failures.append(time.time())
        while(time.time() - self.failures[0] > self.error_window): self.failures.popleft()
        if(self.failures.count() > self.max_errors):
            self.open()

    def execute(self, func, *args, **kwargs):
        if(self.state == CircuitBreakerStates.OPEN):
            raise AlreadyOpenError()
        try:
            result = func(*args, **kwargs)
            self.success()
            return result
        except Exception as e:
            self.failure()
            raise
class ResilientCaller:
    def __init__(self, max_retries=3, jitter=0.1):
        self.max_retries = max_retries
        self.circuit_breaker = CircuitBreaker()
        self.retry_policy = RetryPolicy(max_retries, jitter)

    def execute(self, func, *args, **kwargs):
        def retry(func, *args, **kwargs):
            return self.retry_policy.execute(func, *args, **kwargs)
        return self.circuit_breaker.execute(retry)

