"""Signal histories contain opaque references, never source posts or profiles."""
from dataclasses import dataclass
import math
import re


@dataclass(frozen=True)
class SignalInput:
    signal_id: str
    heartbeat_interval_seconds: float = 10

    def validate(self):
        if not isinstance(self.signal_id, str) or not re.fullmatch('[0-9a-f]{64}', self.signal_id):
            raise ValueError('Signal input must be a SHA-256 reference')
        if (type(self.heartbeat_interval_seconds) not in (int, float)
                or not 0 < self.heartbeat_interval_seconds <= 60
                or not math.isfinite(self.heartbeat_interval_seconds)):
            raise ValueError('Invalid signal Activity heartbeat interval')
        return self


@dataclass(frozen=True)
class SignalResult:
    signal_id: str
    state: str
    count: int = 0


@dataclass(frozen=True)
class SignalCommand:
    signal_id: str
    models_queue: str
    discovery_queue: str
    model_attempt_seconds: float
    publish_attempt_seconds: float
    heartbeat_seconds: float
    heartbeat_interval_seconds: float

    def validate(self):
        SignalInput(self.signal_id, self.heartbeat_interval_seconds).validate()
        for queue in (self.models_queue, self.discovery_queue):
            if not isinstance(queue, str) or not queue.strip() or len(queue.encode()) > 255:
                raise ValueError('Invalid signal task queue')
        for seconds in (self.model_attempt_seconds, self.publish_attempt_seconds, self.heartbeat_seconds):
            from datetime import timedelta
            try:
                valid = type(seconds) in (int, float) and seconds > 0 and math.isfinite(seconds)
                if valid:
                    timedelta(seconds=seconds)
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError('Signal timeouts must be finite positive representable durations')
        if self.heartbeat_seconds <= self.heartbeat_interval_seconds:
            raise ValueError('Signal heartbeat timeout must exceed heartbeat interval')
        return self
