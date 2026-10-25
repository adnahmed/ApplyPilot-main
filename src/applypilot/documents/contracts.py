"""Document identities only: document bodies never enter Workflow history."""
from dataclasses import dataclass
from datetime import timedelta
import math
import re


@dataclass(frozen=True)
class PrepareDocument:
    job_reference: str
    stage: str

    def validate(self):
        if not isinstance(self.job_reference, str) or not re.fullmatch(r'[0-9a-f]{64}', self.job_reference):
            raise ValueError('Invalid job input reference')
        if self.stage not in {'tailor', 'cover'}:
            raise ValueError('Unsupported document purpose')
        return self


@dataclass(frozen=True)
class PreparedDocument:
    document_id: str | None
    state: str
    doc_format: str


@dataclass(frozen=True)
class RenderDocument:
    document_id: str
    doc_format: str
    heartbeat_interval_seconds: float = 10

    def validate(self):
        if not isinstance(self.document_id, str) or not re.fullmatch(r'[0-9a-f]{64}', self.document_id):
            raise ValueError('Invalid document identity')
        if self.doc_format not in {'pdf', 'docx'}:
            raise ValueError('Unsupported document format')
        interval = self.heartbeat_interval_seconds
        if type(interval) not in (int, float) or not 0 < interval <= 60 or not math.isfinite(interval):
            raise ValueError('Invalid heartbeat interval')
        return self


@dataclass(frozen=True)
class DocumentResult:
    document_id: str
    state: str
    artifact_id: str | None = None


@dataclass(frozen=True)
class DocumentCommand:
    document_id: str
    doc_format: str
    task_queue: str
    start_to_close_seconds: float
    heartbeat_seconds: float
    heartbeat_interval_seconds: float

    def validate(self):
        RenderDocument(self.document_id, self.doc_format, self.heartbeat_interval_seconds).validate()
        return _validate_execution(self)


@dataclass(frozen=True)
class JobDocumentCommand:
    job_reference: str
    stage: str
    task_queue: str
    start_to_close_seconds: float
    heartbeat_seconds: float
    heartbeat_interval_seconds: float

    def validate(self):
        PrepareDocument(self.job_reference, self.stage).validate()
        return _validate_execution(self)


def _validate_execution(command):
    if (not isinstance(command.task_queue, str) or not command.task_queue.strip()
            or len(command.task_queue.encode()) > 255):
        raise ValueError('Invalid document task queue')
    for seconds in (command.start_to_close_seconds, command.heartbeat_seconds):
        try:
            valid = type(seconds) in (int, float) and seconds > 0 and math.isfinite(seconds)
            if valid:
                timedelta(seconds=seconds)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError('Document timeouts must be finite positive representable durations')
    interval = command.heartbeat_interval_seconds
    if type(interval) not in (int, float) or not 0 < interval <= 60 or not math.isfinite(interval):
        raise ValueError('Invalid document heartbeat interval')
    if command.heartbeat_seconds <= interval:
        raise ValueError('Document heartbeat timeout must exceed its interval')
    return command
