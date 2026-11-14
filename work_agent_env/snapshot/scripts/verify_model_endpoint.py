"""Preflight by default; --execute makes at most three model requests."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integrations.model_endpoint import ConfigurationMissing, EndpointError, SCOPE, load_private_environment, load_settings, run_probe


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=("openai", "openai_compatible", "claude"))
    parser.add_argument("--config", type=Path, default=ROOT / "configs/model_endpoints.example.json")
    parser.add_argument("--private-env", type=Path, help="Ignored JSON under this project's .runtime; never put a key on the command line")
    parser.add_argument("--execute", action="store_true", help="Send bounded requests to the explicitly configured endpoint")
    parser.add_argument("--stream",action="store_true",help="Use native SSE for Responses or Claude Messages")
    args = parser.parse_args(argv)
    result = {"schema_version": "model-endpoint-preflight-v1", "scope": SCOPE, "profile": args.profile,
              "network_requested": args.execute, "http_requests_attempted": 0, "office_episodes": 0, "training_updates": 0}
    try:
        environment = load_private_environment(ROOT, args.private_env) if args.private_env else None
        settings = load_settings(args.config, args.profile, environ=environment)
        if args.execute:
            result = {**run_probe(settings, ROOT / ".runtime/model-endpoint-probes" / uuid.uuid4().hex,stream=args.stream), "profile": args.profile}
        else:
            result.update(status="configuration_ready_not_executed", protocol=settings.protocol,
                          requested_model=settings.model, endpoint_sha256=hashlib.sha256(settings.base_url.encode()).hexdigest())
    except ConfigurationMissing as exc:
        result.update(status=exc.code, missing_environment=exc.names)
    except EndpointError as exc:
        result.update(status="failed", error_code=exc.code)
    except (OSError, ValueError, TypeError):
        result.update(status="failed", error_code="local_configuration_or_evidence_error")
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["status"] in ("passed", "configuration_ready_not_executed") else 2


if __name__ == "__main__": raise SystemExit(main())
