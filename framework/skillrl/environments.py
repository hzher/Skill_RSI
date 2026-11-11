"""Task environments; local mode exposes file tools only, Docker adds shell tools."""
from pathlib import Path
import json
import subprocess
import uuid
import os
from .storage import atomic
from .storage import contained

class DirectoryEnvironment:
    def __init__(self, workspace):
        self.workspace = Path(workspace)

    def start(self):
        return self

    def close(self):
        pass

    def read(self, name):
        path = contained(self.workspace, name)
        if path.stat().st_size > 2_000_000:
            raise ValueError('Read byte budget exceeded')
        return path.read_text()

    def write(self, name, content):
        if not isinstance(content, str) or len(content.encode()) > 2_000_000:
            raise ValueError('Write byte budget exceeded')
        path = contained(self.workspace, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return {'written': name, 'bytes': len(content.encode())}

class DockerEnvironment(DirectoryEnvironment):
    def __init__(self, workspace, image, *, network='none', user=None, security_profile='hardened'):
        super().__init__(workspace)
        if security_profile not in ('hardened', 'platform'):
            raise ValueError('Explicit Docker security profile required')
        self.security_profile = security_profile
        self.image, self.network = image, network
        self.user = user or (str(os.getuid()) + ':' + str(os.getgid()) if os.getuid() else '1000:1000')
        self.container = None
        self.skill_mount = None
        self.cli_mount = None

    def command(self, args, timeout=30):
        result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError('Docker command failed; exit=' + str(result.returncode) + ' ' + result.stderr[-1500:])
        return result.stdout

    def start(self):
        uid, gid = map(int, self.user.split(':'))
        if os.getuid() == 0:
            for item in [self.workspace, *self.workspace.rglob('*')]:
                os.chown(item, uid, gid)
        elif uid != os.getuid():
            raise ValueError('Docker task UID must match the workspace owner')
        mounts = []
        security = ['--cap-drop', 'ALL', '--security-opt', 'no-new-privileges'] if self.security_profile == 'hardened' else []
        if self.cli_mount:
            mounts += ['--mount', 'type=bind,source=' + str(self.cli_mount.resolve()) + ',target=/opt/skillrl-claude,readonly']
        if self.skill_mount:
            mounts += ['--mount', 'type=bind,source=' + str(self.skill_mount.resolve()) + ',target=/opt/skillrl-skills,readonly']
        self.container = self.command(['create', '--name', 'skillrl-' + uuid.uuid4().hex[:16],
            '--label', 'skillrl.workspace=' + str(self.workspace.resolve()),
            '--network', self.network, '--cpus', '2', '--memory', '4g', '--pids-limit', '256',
            *security, '--user', self.user,
            '--mount', 'type=bind,source=' + str(self.workspace.resolve()) + ',target=/workspace',
            '--workdir', '/workspace', *mounts, self.image, 'sleep', 'infinity']).strip()
        atomic(self.workspace.parent / 'environment.json', self.metadata())
        self.command(['start', self.container])
        return self

    def bash(self, command):
        result = subprocess.run(['docker', 'exec', self.container, 'sh', '-lc', command],
                                capture_output=True, text=True, timeout=120)
        if len(result.stdout) + len(result.stderr) > 2_000_000:
            raise ValueError('Shell output byte budget exceeded')
        return {'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}

    def close(self):
        if self.container:
            self.command(['stop', '-t', '2', self.container])
            atomic(self.workspace.parent / 'environment-stop.json', {'container_id': self.container, 'exit_code': 0})

    def metadata(self):
        return {'container_id': self.container, 'image': self.image, 'workspace': '/workspace',
                'user': self.user, 'network': self.network, 'security_profile': self.security_profile}

    def retire(self):
        from .storage import files, load
        episode = self.workspace.parent
        if files(episode / 'public-artifacts') != load(episode / 'artifact-manifest.json'):
            raise ValueError('Cannot retire without verified complete archived workspace')
        info = json.loads(self.command(['inspect', self.container]))[0]
        if info['State']['Running'] or info['Config'].get('Labels', {}).get('skillrl.workspace') != str(self.workspace.resolve()):
            raise ValueError('Cannot retire an active or differently owned container')
        self.command(['rm', self.container])
        atomic(episode / 'environment-retired.json', {'container_id': self.container, 'exit_code': 0,
                                                     'archived_files': files(episode / 'public-artifacts')})
