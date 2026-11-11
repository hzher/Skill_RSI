"""Small durable IO and identity helpers; no credentials in audit output."""
from pathlib import Path
import hashlib,json,os,tempfile

def canonical(obj):return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(obj):return hashlib.sha256(canonical(obj)).hexdigest()
def file_sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def atomic(path,obj):
 p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
 fd,tmp=tempfile.mkstemp(prefix=p.name+'.',suffix='.partial',dir=p.parent)
 try:
  with os.fdopen(fd,'wb') as f:f.write(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False).encode()+b'\n');f.flush();os.fsync(f.fileno())
  os.replace(tmp,p)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
def immutable(path,raw):
 p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
 try:fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 except FileExistsError:
  if p.read_bytes()!=raw:raise ValueError('Immutable evidence conflict: '+str(p))
  return
 with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
def load(path):return json.loads(Path(path).read_text())
