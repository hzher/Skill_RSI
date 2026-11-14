"""Validate portable runtime declarations without starting services or loading dataset code."""
from pathlib import Path


def asset(package, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("Package assets must use relative paths")
    path = (Path(package) / relative).resolve()
    if not path.is_relative_to(Path(package).resolve()) or not path.exists():
        raise ValueError("Package asset is missing or escapes its root")
    return path


def argv(value):
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        raise ValueError("Command must be a nonempty argv list")
    return value


def validate_runtime(task, package):
    if task["environment"]["backend"] != "docker" or task["evaluation"]["backend"] != "command_json":
        raise ValueError("This runtime supports docker environments and command_json evaluators")
    config = task["environment"]["config"]
    allowed = {"image", "tools", "mcp_servers", "services", "preprocess", "initial_workspace", "mounts", "timeout", "network_access", "env", "inherit_env"}
    if set(config) - allowed or not isinstance(config.get("image"), str) or not config["image"] or config["image"].startswith("-"):
        raise ValueError("Invalid environment declaration")
    if type(config.get("network_access", False)) is not bool:
        raise ValueError("network_access must be boolean")
    if not isinstance(config.get("env", {}), dict) or any(not isinstance(key, str) or not key or "=" in key for key in config.get("env", {})):
        raise ValueError("Invalid environment variables")
    if not isinstance(config.get("inherit_env", []), list) or any(not isinstance(key, str) or not key or "=" in key for key in config.get("inherit_env", [])):
        raise ValueError("Invalid inherited environment variable names")
    for key in ("tools", "mcp_servers", "services", "mounts"):
        if not isinstance(config.get(key, []), list):
            raise ValueError("Runtime collections must be lists")
    if type(config.get("timeout", 180)) not in (int, float) or not 0 < config.get("timeout", 180) <= 3600:
        raise ValueError("Invalid environment timeout")
    names = set()
    for tool in config.get("tools", []):
        if not isinstance(tool, dict) or set(tool) != {"name", "description", "parameters", "argv"}:
            raise ValueError("Invalid command tool")
        if not isinstance(tool["name"], str) or not tool["name"] or tool["name"] in names:
            raise ValueError("Duplicate or invalid tool name")
        names.add(tool["name"])
        argv(tool["argv"])
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(tool["parameters"])
    for server in config.get("mcp_servers", []):
        if not isinstance(server, dict) or set(server) != {"name", "argv"} or server["name"] in names:
            raise ValueError("Invalid MCP server")
        names.add(server["name"])
        argv(server["argv"])
    for service in config.get("services", []):
        if not isinstance(service, dict) or set(service) - {"name", "image", "env", "mounts", "ready"} or not {"name", "image"} <= set(service):
            raise ValueError("Invalid resource service")
        if not isinstance(service["name"], str) or not service["name"].replace("-", "").isalnum():
            raise ValueError("Invalid service name")
        if not isinstance(service["image"], str) or not service["image"] or service["image"].startswith("-") or not isinstance(service.get("env", {}), dict):
            raise ValueError("Invalid service image or environment")
        if "ready" in service:
            argv(service["ready"])
        for mount in service.get("mounts", []):
            validate_mount(package, mount, service=True)
    for mount in config.get("mounts", []):
        validate_mount(package, mount)
    if "initial_workspace" in config:
        if not asset(package, config["initial_workspace"]).is_dir():
            raise ValueError("Initial workspace must be a directory")
    if "preprocess" in config:
        argv(config["preprocess"])
    evaluation = task["evaluation"]["config"]
    if set(evaluation) - {"argv", "mounts", "timeout"} or "argv" not in evaluation:
        raise ValueError("Invalid evaluator declaration")
    argv(evaluation["argv"])
    for mount in evaluation.get("mounts", []):
        validate_mount(package, mount)
    if type(evaluation.get("timeout", 180)) not in (int, float) or not 0 < evaluation.get("timeout", 180) <= 3600:
        raise ValueError("Invalid evaluator timeout")


def validate_mount(package, mount, *, service=False):
    if not isinstance(mount, dict) or set(mount) != {"source", "target"}:
        raise ValueError("Mount requires source and target")
    asset(package, mount["source"])
    target = mount["target"]
    if not isinstance(target, str) or not target.startswith("/") or target == "/" or ".." in Path(target).parts or ":" in target or "," in target:
        raise ValueError("Invalid read-only package mount target")
    if not service and not target.startswith("/assets/"):
        raise ValueError("Agent/evaluator package mounts must be under /assets/")
