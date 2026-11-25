"""Finalize one persisted generation even after its browser or Manager exits."""

import argparse
from contextlib import contextmanager
import fcntl
from pathlib import Path
import time

from ..evidence.io import file_lock, inside, read_json


@contextmanager
def supervisor_lock(output):
    """Only one process watches a job; source locks serialize actual publication."""
    with inside(output, ".supervisor.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def supervise(task, job_id, tasks, *, interval=1.0):
    """Watch exactly this job, never whichever job currently occupies the UI."""
    from .workflow import _now, _paths, _save, poll
    from .snapshot import visible

    task = Path(task).expanduser().resolve(strict=True)
    output, _ = _paths(task, job_id)
    with supervisor_lock(output) as acquired:
        if not acquired:
            return None
        try:
            with file_lock(inside(task, ".emergence.lock")):
                record = read_json(inside(output, "job.json"))
                if not record.get("finalized_at"):
                    record.update(finalizer_started_at=_now(), finalizer_error=None)
                    _save(task, record)
            while True:
                result = poll(task, tasks, job_id=job_id)
                if result.get("finalized_at") or not result.get("analysis_job_id"):
                    return result
                time.sleep(interval)
        except Exception as error:
            with file_lock(inside(task, ".emergence.lock")):
                record = read_json(inside(output, "job.json"))
                record["finalizer_error"] = visible(str(error))
                _save(task, record)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--tasks-dir", required=True)
    args = parser.parse_args(argv)
    from ..tasks.store import TaskStore

    tasks = TaskStore(args.tasks_dir, state_dir=args.state_dir)
    supervise(args.task, args.job_id, tasks)


if __name__ == "__main__":
    main()
