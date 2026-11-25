"""One native Codex job from recorded trajectory to reviewable harness packages."""

from pathlib import Path
import time

from . import command


def _run(args):
    from ..discovery.codex_settings import get_settings
    from ..emergence import poll, start
    from ..tasks import TaskStore

    tasks = TaskStore(state_dir=args.state_dir)
    selected = Path(args.task).expanduser()
    source = selected.resolve() if selected.is_dir() else tasks.path(args.task)
    if args.status:
        return poll(source, tasks)
    settings = get_settings(args.state_dir)
    options = {key: getattr(args, key) or settings[key] for key in ("model", "effort", "command")}
    result = start(source, tasks, args.state_dir, instructions=args.instructions, **options)
    while args.wait and result.get("status") in {"preparing", "starting", "queued", "running", "cancelling"}:
        time.sleep(2)
        result = poll(source, tasks)
    return result


def register(routes):
    parser = command(routes, "emerge", "Let local Codex discover and write harness extension candidates", _run)
    parser.add_argument("--task", required=True, help="task ID or recorded task directory")
    parser.add_argument("--instructions", default="", help="optional research focus; no fixed strategy is imposed")
    parser.add_argument("--model", help="default: gpt-6.1-sol, or saved Codex generation model")
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max", "ultra"))
    parser.add_argument("--command", help="native Codex executable")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="read the current generation job and candidate paths")
    mode.add_argument("--wait", action="store_true", help="wait for generation and static packaging to finish")
