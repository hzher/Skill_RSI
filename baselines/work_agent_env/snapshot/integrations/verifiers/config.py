"""Explicit deployment configuration for task-set verifiers (never taken from model output)."""
from pathlib import Path

from integrations.benchmark_tasks import digest, strict_json
from .adapters import CommandJSONVerifier
from .service import VerifierRegistry, VerifierService


def configured_service(path):
    config = strict_json(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict) or set(config) != {"schema_version", "backends"} or config["schema_version"] != "workagent-verifiers-v1":
        raise ValueError("Invalid verifier deployment configuration")
    registry = VerifierRegistry()
    for name, settings in config["backends"].items():
        if set(settings) != {"argv", "cwd", "files", "timeout"}:
            raise ValueError("Invalid verifier backend configuration")
        registry.register(name, CommandJSONVerifier(**settings), version=digest(settings))
    return VerifierService(registry)
