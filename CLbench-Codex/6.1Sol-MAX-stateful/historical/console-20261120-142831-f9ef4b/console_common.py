"""Local console IO and bounded paths; no benchmark or account side effects."""
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import tempfile
import time

APP = Path(__file__).resolve().parent
BASE = APP.parent
STATE = APP/'state'
EXPERIMENTS = BASE.parent/'experiments'
LEGACY_ID = 'baseline-20261118'


class UserError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message); self.status=status


def read(path, default=None):
    if not Path(path).exists(): return default
    for attempt in range(3):
        try:return json.loads(Path(path).read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            if attempt == 2:return default
        except json.JSONDecodeError:
            if attempt == 2:raise UserError('记录正在写入或格式异常，请稍后刷新。',503)
        time.sleep(.02)


def atomic(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    raw=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
    fd,name=tempfile.mkstemp(prefix=path.name+'.',suffix='.partial',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as handle:
            handle.write(raw);handle.flush();os.fsync(handle.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def frozen(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')+b'\n'
    try:
        with path.open('xb') as handle:handle.write(raw);handle.flush();os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes()!=raw:raise UserError('已有冻结配置不同，必须新建独立运行。',409)


def digest(path):
    with Path(path).open('rb') as handle:return hashlib.file_digest(handle,'sha256').hexdigest()


def safe_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}',value):
        raise UserError('无效的记录标识。')
    return value


def contained(root,name):
    root=Path(root).resolve()
    if not isinstance(name,str) or '\\' in name or ':' in name or '\0' in name:
        raise UserError('文件路径不在允许范围内。',403)
    part=Path(name)
    if part.is_absolute() or '..' in part.parts:raise UserError('文件路径不在允许范围内。',403)
    current=root
    for piece in part.parts:
        current=current/piece
        if current.exists() or current.is_symlink():
            attributes=getattr(current.lstat(),'st_file_attributes',0)
            if current.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise UserError('不读取符号链接或重解析路径。',403)
    if not current.resolve().is_relative_to(root):raise UserError('文件路径越界。',403)
    return current


def linux_path(value):
    value=str(value)
    if value.startswith('/'):return value
    path=PureWindowsPath(value)
    if not re.fullmatch(r'[A-Za-z]:',path.drive):raise UserError('需要本机盘符路径。')
    return '/mnt/'+path.drive[0].lower()+'/'+'/'.join(path.parts[1:])


def verified_cli_copy(original, state=STATE, experiments=EXPERIMENTS):
    """Locate an identical local binary after the editor removes its versioned directory."""
    local = Path(original if os.name == 'nt' else linux_path(original))
    if local.is_file():
        return None
    for receipt_path in (state/'native-tools').glob('*/receipt.json'):
        receipt = read(receipt_path)
        if receipt.get('original_cli') != original:
            continue
        expected = receipt_path.parent.name
        if not re.fullmatch('[a-f0-9]{64}', expected) or receipt.get('cli_sha256') != expected:
            raise UserError('恢复的 Codex 可执行文件缺少可核验的摘要。')
        recorded_path = receipt['frozen_invocation']
        source = Path(recorded_path if os.name == 'nt' else linux_path(recorded_path)).resolve()
        if source.name != 'actor-config.json' or not source.is_relative_to(experiments.resolve()):
            raise UserError('Codex 原始执行记录不在实验目录。')
        actor = read(source, {}) or {}
        binary = receipt_path.with_name('codex.exe')
        if actor.get('cli') != original or actor.get('cli_sha256') != expected:
            raise UserError('恢复的 Codex 文件与原始执行记录的 SHA-256 不匹配。')
        files = receipt.get('platform_file_manifest') or {}
        required = {'codex.exe','codex-code-mode-host.exe','codex-command-runner.exe','codex-windows-sandbox-setup.exe','rg.exe'}
        if not required.issubset(files) or files['codex.exe'].get('sha256') != expected:
            raise UserError('恢复的 Codex 缺少官方平台工具清单。')
        for name, item in files.items():
            path = contained(binary.parent, name)
            if not path.is_file() or path.stat().st_size != item.get('bytes') or digest(path) != item.get('sha256'):
                raise UserError('Codex 平台工具缺失或摘要不同：'+name)
        return binary
    return None


def clean_error(value):
    text=str(value)
    text=re.sub(r'sk-[A-Za-z0-9_-]{16,}','[REDACTED]',text)
    text=re.sub(r'Bearer\s+[A-Za-z0-9_.-]+','Bearer [REDACTED]',text,flags=re.I)
    return text[:1200]
