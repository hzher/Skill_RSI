"""Keep account credentials on the host when Codex runs in benchmark containers."""
import json
from pathlib import Path

from ..gateway import WireRecorder


class AccountRelay(WireRecorder):
    def __init__(self, output, *, auth_path=None, port=0, timeout=300):
        self.auth_path = Path(auth_path or Path.home() / '.codex/auth.json')
        super().__init__('codex', 'https://chatgpt.com/backend-api/codex', output,
                         port=port, timeout=timeout, bind_host='0.0.0.0')

    def upstream_headers(self, headers):
        # Read on each request so a refresh by the native host CLI is picked up.
        auth = json.loads(self.auth_path.read_text())
        token = (auth.get('tokens') or {}).get('access_token')
        account = (auth.get('tokens') or {}).get('account_id')
        if auth.get('auth_mode') != 'chatgpt' or not token or not account:
            raise ValueError('native_account_authentication_unavailable')
        return {**headers, 'Authorization': 'Bearer ' + token, 'ChatGPT-Account-Id': account}

    @property
    def container_url(self):
        return self.base_url.replace('127.0.0.1', 'host.docker.internal')


class ApiRelay(WireRecorder):
    """Forward containers through an explicit API route with a host-only key file."""
    def __init__(self, upstream, output, *, credential_file, port=0, timeout=300, proxy_mode='environment'):
        if proxy_mode not in {'environment', 'direct'}:
            raise ValueError('proxy_mode must be environment or direct')
        self.proxy_mode = proxy_mode
        self.credential_file = Path(credential_file)
        self._credential()  # Fail before opening a listener if the key is missing.
        super().__init__('codex', upstream, output, port=port, timeout=timeout, bind_host='0.0.0.0')

    def open_upstream(self, parsed, timeout):
        from .transport import connection
        return connection(parsed, timeout, self.proxy_mode)

    def _credential(self):
        key = self.credential_file.read_text().strip()
        if not key or any(char.isspace() for char in key):
            raise ValueError('API credential file must contain one nonempty key')
        return key

    def upstream_headers(self, headers):
        # Read each request so rotation does not require copying a secret into containers.
        selected = {name: value for name, value in headers.items()
                    if name.lower() not in {'authorization', 'chatgpt-account-id', 'x-api-key'}}
        return {**selected, 'Authorization': 'Bearer ' + self._credential()}

    @property
    def container_url(self):
        return self.base_url.replace('127.0.0.1', 'host.docker.internal')
