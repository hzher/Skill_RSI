"""Use the same restricted Windows Codex backend with a named family thread."""
import json
import os
from pathlib import Path
import sys
import uuid
from unittest.mock import patch


def session_arguments(original, spec):
    args = original(spec)
    args.remove('--ephemeral')
    args[args.index('-C') + 1] = spec['family_workspace']
    session = spec.get('resume_thread_id')
    if session:
        if str(uuid.UUID(session)) != session:
            raise ValueError('Invalid stored native thread id')
        # resume has its own model/JSON/output options; sandbox and cwd belong
        # to the enclosing exec command in this pinned CLI.
        common = args[2:-1]
        parent = args[:2]
        for key in ('-C', '-s', '--color'):
            position = common.index(key)
            parent += common[position:position+2]
            del common[position:position+2]
        return parent + ['resume', *common, '-c', 'sandbox_mode="read-only"', session, '-']
    return args


def main():
    spec = json.load(sys.stdin)
    if 'framework_path' in spec:
        framework = Path(spec['framework_path']).resolve(strict=True)
    else:
        from console_common import BASE
        framework = BASE/'sources/Skill_RSI/framework'
    sys.path.insert(0, str(framework))
    from skillrl import codex_host_actor
    workspace = Path(spec['family_workspace']).resolve(strict=True)
    os.chdir(workspace)
    original = codex_host_actor.actor_arguments
    with patch.object(codex_host_actor, 'actor_arguments', lambda value: session_arguments(original, value)):
        return codex_host_actor.run(spec)


if __name__ == '__main__':
    raise SystemExit(main())
