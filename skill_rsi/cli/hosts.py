"""Host lifecycle and independent project bindings."""
from argparse import REMAINDER
from pathlib import Path

from . import command, common_host, config_arg


def _adapter(name):
    from ..adapters.registry import get_adapter
    return get_adapter(name)


def _runtime(args):
    from ..runtime import Runtime
    return Runtime(args.project, state_dir=args.state_dir)


def _probe(args, host, name):
    probe = host.probe(args.project, args.state_dir, command=args.command)
    return {"host": name, "probe": probe, "runtime": _runtime(args).status(name)}


def _doctor(args):
    from ..adapters.registry import available
    names = [args.host] if args.host else available()
    return {"project": args.project.resolve(), "state_dir": args.state_dir, "hosts": [
        _probe(args, _adapter(name), name) for name in names
    ]}


def _setup_options(args, *, attach=False):
    from ..arguments.config import load_config

    config = load_config(args.config) if args.config else {}
    if "source" in config:
        config.setdefault("host", config.pop("source").get("host"))
    args.host = args.host or config.pop("host", None)
    config.pop("host", None)
    project = config.pop("project", None)
    if project:
        args.project = (args.config.resolve().parent / project).resolve()
    if not args.host:
        raise ValueError("a host is required")
    if config.pop("mode", "observe") != "observe":
        raise ValueError("use project enable to select a component mode")
    if attach:
        config["observe"] = not args.stop
    if args.command:
        config["command"] = args.command
    if args.profile:
        config["profile"] = args.profile
    return config


def _setup(args):
    options = _setup_options(args)
    result = _adapter(args.host).attach(args.project, args.state_dir, **options)
    if "observe" in options:
        _runtime(args).set_observe(args.host, options["observe"])
    return result


def _attach(args):
    options = _setup_options(args, attach=True)
    result = _adapter(args.host).attach(args.project, args.state_dir, **options)
    _runtime(args).set_observe(args.host, options["observe"])
    return result


def _uninstall(args):
    runtime = _runtime(args)
    state = runtime.status(args.host)
    if any(binding["enabled"] for binding in (*state["bindings"], *state["effective_bindings"])):
        raise ValueError("disable project bindings and confirm the next host boundary before uninstalling")
    removed = _adapter(args.host).detach(args.project, args.state_dir)
    runtime.set_observe(args.host, False)
    return removed


def _collect(args):
    from ..arguments.config import load_config

    options = load_config(args.config) if args.config else {}
    if args.command:
        options["command"] = args.command
    if args.trace:
        if args.host != "codex":
            raise ValueError("native rollout trace is a Codex capture option")
        options["trace"] = True
    for key in ("model", "timeout", "output"):
        if getattr(args, key) is not None:
            options[key] = getattr(args, key)
    argv = args.argv[1:] if args.argv and args.argv[0] == "--" else args.argv
    launch = _adapter(args.host).launch(argv or None, project=args.project, state_dir=args.state_dir,
                                        prompt=args.prompt, resume=args.resume, options=options)
    if args.archive and launch.get("path"):
        from ..pipelines.evidence import ingest
        launch["archive"] = ingest(Path(launch["path"]), output=args.archive, host=args.host,
                                   format=launch.get("capture_format"), seal=True)
    return launch


def _gateway(args):
    from ..adapters.gateway import main
    argv = ["--host", args.host, "--upstream", args.upstream, "--output", str(args.output),
            "--port", str(args.port)]
    if args.credential_env:
        argv.extend(("--credential-env", args.credential_env))
    main(argv)
    return {"status": "stopped", "output": args.output}


def _reference(value):
    component, separator, version = value.rpartition("@")
    if not separator or not component or not version:
        raise ValueError("select a version as component-id@version")
    return component, version


def _artifacts(args):
    from ..components import ComponentRegistry, pack, pack_builtin

    registry = ComponentRegistry(args.state_dir)
    match args.action:
        case "pack":
            return pack(args.source, args.output)
        case "builtin":
            return pack_builtin(args.component, args.output, form=args.form)
        case "install" | "upgrade":
            return registry.install(args.file)
        case "uninstall":
            return registry.uninstall(*_reference(args.release), args.form)
        case "list":
            return [{**release.to_dict(), "bindings": registry.references(release),
                     "reports": registry.reports(release)} for release in registry.list()]
        case "report":
            from ..evidence.io import read_json

            release = registry.resolve(*_reference(args.release), args.form)
            report = read_json(args.file)
            matches = [row for row in report.get("comparisons", [])
                       if release.digest in row.get("artifact_digests", ())]
            if not matches:
                raise ValueError("experiment report does not reference these release bytes")
            return [registry.record_report(release, {
                **row, "artifact_digest": release.digest, "protocol_id": report["protocol_id"],
                "protocol_sha256": report["protocol_sha256"],
            }) for row in matches]


def _projects(args):
    runtime = _runtime(args)
    if args.action == "list":
        return runtime.bindings(args.host)
    if args.action == "receipts":
        return runtime.decisions(args.host, session_id=args.session_id)
    if args.action == "expand":
        return runtime.expand(args.handle, args.start_line, args.end_line)
    if args.action == "adopt":
        return runtime.accept_suggestion(args.operation_key, receipt_ref=args.receipt_ref, accepted=not args.decline)
    host = _adapter(args.host)
    capabilities = host.probe(args.project, args.state_dir, command=args.command)
    if args.action == "disable":
        return runtime.disable(args.host, args.component, capabilities=capabilities,
                               expected_revision=args.expected_revision)
    from ..arguments.config import load_config
    return runtime.enable(args.host, *_reference(args.component), form=args.form, mode=args.mode,
                          capabilities=capabilities, expected_revision=args.expected_revision,
                          options=load_config(args.config) if args.config else None)


def register(routes):
    common_host(command(routes, "doctor", "Report actual host and project capability evidence", _doctor), required=False)
    parser = command(routes, "setup", "Register a CLI adapter for a project", _setup)
    common_host(parser, required=False)
    config_arg(parser)
    parser.add_argument("--profile")
    parser = command(routes, "attach", "Enable or stop independent observation", _attach)
    common_host(parser, required=False)
    config_arg(parser)
    parser.add_argument("--profile")
    parser.add_argument("--mode", choices=["observe"], default="observe")
    parser.add_argument("--stop", action="store_true")
    common_host(command(routes, "uninstall", "Remove a managed host registration", _uninstall))
    parser = command(routes, "collect", "Start or resume an isolated native CLI capture", _collect)
    common_host(parser)
    config_arg(parser)
    parser.add_argument("--prompt")
    parser.add_argument("--resume")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--trace", action="store_true", help="enable native Codex rollout trace for this launch")
    parser.add_argument("--argv", nargs=REMAINDER, help="explicit native command arguments instead of the default entry point")
    parser.set_defaults(exit_status=lambda response: 0 if response["status"] == "completed" else 1)

    parser = command(routes, "gateway", "Record explicitly routed Claude Messages or Codex Responses", _gateway)
    parser.add_argument("--host", choices=("claude_code", "codex"), required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--credential-env")
    parser.add_argument("--port", type=int, default=0)

    group = command(routes, "artifact", "Package and manage independently versioned strategy artifacts")
    sub = group.add_subparsers(dest="action", required=True)
    for action in ("pack", "builtin", "install", "upgrade", "uninstall", "list", "report"):
        parser = command(sub, action, f"{action.capitalize()} strategy artifacts", _artifacts)
        if action == "pack":
            parser.add_argument("source", type=Path)
        if action == "builtin":
            parser.add_argument("component", choices=("tool-result-view", "constraint-check"))
        if action in {"pack", "builtin"}:
            parser.add_argument("--output", required=True, type=Path)
        if action in {"install", "upgrade", "report"}:
            parser.add_argument("file", type=Path)
        if action in {"uninstall", "report"}:
            parser.add_argument("release")
        if action in {"builtin", "uninstall", "report"}:
            parser.add_argument("--form")

    group = command(routes, "project", "Inspect or change independent project bindings")
    sub = group.add_subparsers(dest="action", required=True)
    common_host(command(sub, "list", "List project bindings", _projects), required=False)
    parser = command(sub, "receipts", "List recorded single-event decisions and host receipts", _projects)
    common_host(parser, required=False)
    parser.add_argument("--session-id")
    parser = command(sub, "expand", "Read a preserved original tool output", _projects)
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("handle")
    parser.add_argument("--start-line", type=int)
    parser.add_argument("--end-line", type=int)
    parser = command(sub, "adopt", "Record the response to an observed displayed suggestion", _projects)
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("operation_key")
    parser.add_argument("--receipt-ref", required=True)
    parser.add_argument("--decline", action="store_true")
    for action in ("enable", "disable"):
        parser = command(sub, action, f"{action.capitalize()} a project component", _projects)
        common_host(parser)
        parser.add_argument("component")
        parser.add_argument("--expected-revision", type=int)
        if action == "enable":
            parser.add_argument("--mode", choices=("shadow", "suggest", "apply"), default="shadow")
            parser.add_argument("--form")
            config_arg(parser)
