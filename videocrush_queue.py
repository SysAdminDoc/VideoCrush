"""Persistable queue state for VideoCrush desktop and automation workflows."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from videocrush_core import PRESET_PROFILES, VideoCrushError, output_path_for


QUEUE_SCHEMA_VERSION = 1
QUEUE_STATES = frozenset({"pending", "running", "paused", "done", "failed", "cancelled"})


@dataclass
class QueueJob:
    input_path: str
    output_path: str
    preset: str = "web-1080p"
    overrides: Dict[str, object] = field(default_factory=dict)
    priority: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = "pending"
    attempts: int = 0
    error: str = ""
    logs: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.state not in QUEUE_STATES:
            raise VideoCrushError(f"Invalid queue state: {self.state}")
        if self.preset not in PRESET_PROFILES:
            raise VideoCrushError(f"Unknown queue preset: {self.preset}")
        self.input_path = str(Path(self.input_path))
        self.output_path = str(Path(self.output_path))

    @property
    def name(self) -> str:
        return Path(self.input_path).name

    def append_log(self, message: str) -> None:
        self.logs.append(str(message))
        del self.logs[:-200]

    def reset_for_retry(self) -> None:
        self.state = "pending"
        self.error = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "input_path": self.input_path,
            "output_path": self.output_path,
            "preset": self.preset,
            "overrides": self.overrides,
            "priority": self.priority,
            "state": self.state,
            "attempts": self.attempts,
            "error": self.error,
            "logs": self.logs[-200:],
        }

    @classmethod
    def from_dict(cls, value: dict) -> "QueueJob":
        return cls(
            id=str(value.get("id") or uuid.uuid4().hex),
            input_path=str(value["input_path"]),
            output_path=str(value["output_path"]),
            preset=str(value.get("preset", "web-1080p")),
            overrides=dict(value.get("overrides") or {}),
            priority=int(value.get("priority", 0)),
            state=str(value.get("state", "pending")),
            attempts=int(value.get("attempts", 0)),
            error=str(value.get("error", "")),
            logs=[str(item) for item in value.get("logs", [])][-200:],
        )


class JobQueue:
    """Ordered queue with priority-aware selection and durable JSON state."""

    def __init__(self, jobs: Optional[Iterable[QueueJob]] = None) -> None:
        self.jobs: List[QueueJob] = list(jobs or [])

    def add(self, job: QueueJob) -> QueueJob:
        self.jobs.append(job)
        return job

    def add_file(
        self,
        input_path: Path,
        output_dir: Path,
        preset: str = "web-1080p",
        output_format: Optional[str] = None,
        overrides: Optional[Dict[str, object]] = None,
        priority: int = 0,
    ) -> QueueJob:
        input_path = Path(input_path)
        output_path = output_path_for(
            input_path,
            Path(output_dir),
            output_format=output_format or PRESET_PROFILES[preset].output_format,
            multiple=True,
        )
        return self.add(
            QueueJob(
                input_path=str(input_path),
                output_path=str(output_path),
                preset=preset,
                overrides=dict(overrides or {}),
                priority=priority,
            )
        )

    def get(self, job_id: str) -> QueueJob:
        for job in self.jobs:
            if job.id == job_id:
                return job
        raise KeyError(job_id)

    def remove(self, job_id: str) -> QueueJob:
        for index, job in enumerate(self.jobs):
            if job.id == job_id:
                return self.jobs.pop(index)
        raise KeyError(job_id)

    def move(self, job_id: str, delta: int) -> None:
        index = next(index for index, job in enumerate(self.jobs) if job.id == job_id)
        target = max(0, min(len(self.jobs) - 1, index + delta))
        if target == index:
            return
        self.jobs[index], self.jobs[target] = self.jobs[target], self.jobs[index]

    def set_priority(self, job_id: str, priority: int) -> None:
        self.get(job_id).priority = int(priority)

    def next_pending(self) -> Optional[QueueJob]:
        pending = [(index, job) for index, job in enumerate(self.jobs) if job.state == "pending"]
        if not pending:
            return None
        return max(pending, key=lambda item: (item[1].priority, -item[0]))[1]

    def reset_running(self) -> None:
        for job in self.jobs:
            if job.state in {"running", "paused"}:
                job.state = "pending"

    def retry(self, job_id: str) -> None:
        job = self.get(job_id)
        if job.state not in {"failed", "cancelled"}:
            raise VideoCrushError("Only failed or cancelled jobs can be retried.")
        job.reset_for_retry()

    def to_dict(self) -> dict:
        return {"schema_version": QUEUE_SCHEMA_VERSION, "jobs": [job.to_dict() for job in self.jobs]}

    @classmethod
    def from_dict(cls, value: dict) -> "JobQueue":
        if int(value.get("schema_version", 0)) != QUEUE_SCHEMA_VERSION:
            raise VideoCrushError("Unsupported queue file schema version.")
        return cls(QueueJob.from_dict(item) for item in value.get("jobs", []))


class QueueStore:
    """Atomic JSON persistence for queue specs."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> JobQueue:
        if not self.path.is_file():
            return JobQueue()
        try:
            return JobQueue.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, KeyError, VideoCrushError) as exc:
            raise VideoCrushError(f"Could not load queue file {self.path}: {exc}") from exc

    def save(self, queue: JobQueue) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(queue.to_dict(), indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


def default_queue_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "VideoCrush" / "queue.json"
