"""Replay tracer for the agent loop (П3).

Writes JSONL event stream to logs/{ts}_{model}/traces.jsonl.
Each line = one JSON object representing one event from one task step.

Controlled by TRACE_ENABLED=1 env var (default: 0 — no overhead in prod).

Public API:
  init_tracer(log_dir)  — call once per run from main.py after _setup_log_tee()
  get_task_tracer()     — returns TaskTracer for current thread (no-op when disabled)
  set_task_id(task_id)  — bind task_id to current worker thread
  TaskTracer            — per-task tracer, .emit()
"""
import json
import os
import threading
from datetime import datetime, timezone


_TRACE_ENABLED = os.environ.get("TRACE_ENABLED", "0") == "1"

# Module-level singleton: one open JSONL file shared across all tasks in a run.
_tracer_lock = threading.Lock()
_current_tracer: "RunTracer | None" = None

# Thread-local: current task_id per worker thread (set by set_task_id from main.py)
_task_local = threading.local()


# ---------------------------------------------------------------------------
# RunTracer — one JSONL file per benchmark run
# ---------------------------------------------------------------------------

class RunTracer:
    """Append-only JSONL writer. Fail-open: errors in emit() never propagate."""

    def __init__(self, jsonl_path: str) -> None:
        self._lock = threading.Lock()
        try:
            self._fh = open(jsonl_path, "a", encoding="utf-8", buffering=1)
        except OSError:
            self._fh = None

    def emit(self, task_id: str, step_num: int, event: str, data: dict) -> None:
        """Append one event line. Fail-open on any error."""
        if self._fh is None:
            return
        record = {
            "task_id": task_id,
            "step_num": step_num,
            "event": event,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data": data,
        }
        # Serialize outside the lock to reduce contention under PARALLEL_TASKS > 1
        line = json.dumps(record, ensure_ascii=False) + "\n"
        try:
            with self._lock:
                self._fh.write(line)
        except Exception:
            pass

    def close(self) -> None:
        try:
            if self._fh:
                self._fh.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TaskTracer — thin per-task wrapper that binds task_id
# ---------------------------------------------------------------------------

class TaskTracer:
    """Per-task facade over RunTracer. Binds task_id for all emit calls."""

    def __init__(self, run_tracer: "RunTracer | None", task_id: str) -> None:
        self._run = run_tracer
        self._task_id = task_id

    def emit(self, event: str, step_num: int, data: dict) -> None:
        if self._run is not None:
            self._run.emit(self._task_id, step_num, event, data)


# Null Object: returned when tracing is disabled — zero allocation, zero lock per step.
_NULL_TRACER = TaskTracer(None, "")


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def init_tracer(log_dir: str) -> None:
    """Initialise the run-level JSONL tracer. No-op if TRACE_ENABLED != 1."""
    global _current_tracer
    if not _TRACE_ENABLED:
        return
    jsonl_path = os.path.join(log_dir, "traces.jsonl")
    with _tracer_lock:
        _current_tracer = RunTracer(jsonl_path)


def set_task_id(task_id: str) -> None:
    """Store task_id in thread-local so loop.py can access it without signature changes."""
    if not _TRACE_ENABLED:
        return
    _task_local.task_id = task_id


def get_task_tracer(task_id: str = "") -> TaskTracer:
    """Return a TaskTracer bound to task_id (falls back to thread-local if not given).
    Fast-path: returns _NULL_TRACER without any lock when TRACE_ENABLED=0."""
    if not _TRACE_ENABLED:
        return _NULL_TRACER
    tid = task_id or getattr(_task_local, "task_id", "unknown")
    with _tracer_lock:
        run = _current_tracer
    return TaskTracer(run, tid)


def close_tracer() -> None:
    """Flush and close the run-level tracer. Call at process exit."""
    global _current_tracer
    with _tracer_lock:
        if _current_tracer is not None:
            _current_tracer.close()
            _current_tracer = None
