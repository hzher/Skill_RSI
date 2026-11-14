"""Validate or execute an external skill-v1 JSON over the existing NeMo task environment."""
import argparse
import json
import sqlite3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.model_endpoint import EndpointError, load_private_environment, load_settings, strict_json
from integrations.skill_rsi_contract import MAX_REQUEST_BYTES, RSIError, available_tasks, validate_request
from integrations.skill_rsi_runtime import SkillRSI


def load_request(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REQUEST_BYTES:
        raise RSIError('invalid_request_file')
    with path.open('rb') as stream:
        raw = stream.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise RSIError('request_size_limit')
    try:
        return strict_json(raw)
    except EndpointError:
        raise RSIError('invalid_request_json') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--serve', action='store_true')
    mode.add_argument('--compare', action='store_true')
    mode.add_argument('--recover-request-id')
    mode.add_argument('--recover-lock', choices=('campaign','http'))
    parser.add_argument('--token-file', type=Path)
    parser.add_argument('--port', type=int, default=18820)
    parser.add_argument('--profile', choices=('openai', 'claude'), required=True)
    parser.add_argument('--private-env', type=Path, required=True)
    parser.add_argument('--max-steps', type=int, default=100)
    parser.add_argument('--wall-seconds', type=int, default=1800)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    try:
        env = load_private_environment(ROOT, args.private_env)
        settings = load_settings(ROOT/'configs/model_endpoints.example.json', args.profile, env)
        if args.recover_lock:
            if not args.execute or args.request:
                raise RSIError('lock_recovery_requires_execute')
            from integrations.skill_rsi_locks import recover_lock
            service=SkillRSI(ROOT,settings,max_steps=args.max_steps,wall_seconds=args.wall_seconds)
            print(json.dumps(recover_lock(service,args.recover_lock),ensure_ascii=True,allow_nan=False))
            return 0
        if args.recover_request_id:
            if not args.execute or args.request:
                raise RSIError('recovery_requires_execute_and_request_id')
            from integrations.skill_rsi_recovery import reconcile
            service = SkillRSI(ROOT, settings, max_steps=args.max_steps, wall_seconds=args.wall_seconds)
            job = reconcile(service,args.recover_request_id)
            print(json.dumps(job,ensure_ascii=True,allow_nan=False))
            return 0 if job['state'] in ('completed','rejected') else 2
        if args.serve:
            if args.request or not args.execute or not args.token_file or not 1 <= args.port <= 65535:
                raise RSIError('server_requires_execute_and_token_file')
            from integrations.skill_rsi_http import SkillHTTPServer
            from workenv.core import no_links
            from integrations.skill_rsi_locks import rsi_lock
            token_path = args.token_file.absolute()
            no_links(token_path, ROOT)
            token_path = token_path.resolve()
            if not token_path.is_relative_to(ROOT/'.runtime') or token_path.stat().st_size > 256:
                raise RSIError('invalid_server_token_file')
            if sys.platform != 'win32' and token_path.stat().st_mode & 0o077:
                raise RSIError('server_token_file_permissions')
            token = token_path.read_text(encoding='utf-8').strip()
            service = SkillRSI(ROOT, settings, max_steps=args.max_steps, wall_seconds=args.wall_seconds)
            with rsi_lock(service,'http','skill_rsi_http'):
                with SkillHTTPServer(service,token,port=args.port) as server:
                    print(json.dumps({'status':'listening','host':'127.0.0.1','port':args.port,'executor_id':service.executor_id}),flush=True)
                    try:
                        server.serve_forever()
                    except KeyboardInterrupt:
                        pass
                    finally:
                        server.wait_worker()
            return 0
        if args.request is None:
            raise RSIError('request_file_required')
        request = load_request(args.request)
        if not args.compare:
            request = validate_request(request, available_tasks(ROOT))
        else:
            from integrations.skill_rsi_comparison import SkillComparison
            service = SkillRSI(ROOT, settings, max_steps=args.max_steps, wall_seconds=args.wall_seconds)
            comparison = SkillComparison(service)
            request = comparison.validate(request)
        if not args.execute:
            from integrations.skill_rsi_contract import descriptor_id, executor_descriptor
            frozen = executor_descriptor(ROOT, settings, max_steps=args.max_steps, wall_seconds=args.wall_seconds)
            print(json.dumps({'status': 'validated_not_executed', 'request_id': request['request_id'],
                              'executor_id': descriptor_id(frozen), 'task_id': request['task_id'], 'model_calls': 0}))
            return 0
        service = SkillRSI(ROOT, settings, max_steps=args.max_steps, wall_seconds=args.wall_seconds)
        job = comparison.evaluate(request) if args.compare else service.evaluate(request)
        print(json.dumps(job, ensure_ascii=True, allow_nan=False))
        return 0 if job['state']=='completed' else 2
    except (RSIError, EndpointError) as exc:
        print(json.dumps({'error': exc.code}))
        return 2
    except (OSError, ValueError):
        print(json.dumps({'error':'local_configuration_unavailable'}))
        return 2
    except sqlite3.Error:
        print(json.dumps({'error':'local_state_unavailable'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
