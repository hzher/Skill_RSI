"""Observe native Codex CLI and Desktop sessions without launching a task."""

from pathlib import Path

from . import command


def _record(args):
    from ..adapters.codex.recording import configure, import_session, status, sync, watch

    scope = args.scope or ("project" if args.project is not None else "all")
    project = (args.project or Path(".")) if scope == "project" else None
    if args.import_session:
        return import_session(args.import_session, args.state_dir)
    if args.status:
        return status(args.state_dir, project=project)
    result = configure(project, args.state_dir, enabled=not args.disable,
                       codex_home=args.codex_home, tasks_dir=args.tasks_dir,
                       scope="project" if scope == "project" else "all_projects")
    if args.disable:
        return result
    if args.once:
        return sync(args.state_dir, project=project)
    print("正在记录 Codex CLI 与桌面端会话。按 Ctrl+C 停止本次同步；配置会保留。", flush=True)
    try:
        watch(args.state_dir)
    except KeyboardInterrupt:
        pass
    return status(args.state_dir, project=project)


def register(routes):
    parser = command(routes, "record", "Record new activity from Codex CLI and Desktop sessions", _record)
    parser.add_argument("--host", choices=("codex",), required=True)
    parser.add_argument("--scope", choices=("all", "project"), help="record all local projects by default")
    parser.add_argument("--project", type=Path, help="limit recording to this directory unless --scope all is explicit")
    parser.add_argument("--codex-home", type=Path, help="native Codex home (default: CODEX_HOME or ~/.codex)")
    parser.add_argument("--tasks-dir", type=Path, help="unified task folder root (default: ~/SkillRSI/tasks)")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--once", action="store_true", help="synchronize once, then exit")
    actions.add_argument("--status", action="store_true", help="show recording configuration and coverage")
    actions.add_argument("--disable", action="store_true", help="stop recording this scope and keep existing data")
    actions.add_argument("--import-session", metavar="THREAD_ID", help="explicitly import only one user conversation and its internal tasks")
