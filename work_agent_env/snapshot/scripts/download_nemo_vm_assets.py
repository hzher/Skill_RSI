"""Fetch publisher-listed Linux VM prerequisites into the owned runtime directory."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
DIRECTORY=ROOT/".runtime/nemo-vm/assets"


def fetch(url,name,expected=None,publisher_verified=False):
    path=DIRECTORY/name
    if path.exists():
        if path.stat().st_size>1024**3:raise ValueError("Existing asset exceeds 1 GiB budget")
        with path.open("rb") as source:actual=hashlib.file_digest(source,"sha512").hexdigest()
        if expected and actual!=expected:raise ValueError("Existing asset hash mismatch: "+name)
        return {"url":url,"file":path.relative_to(ROOT).as_posix(),"sha512":actual,"bytes":path.stat().st_size,"expected_digest_verified":expected is not None,"publisher_digest_verified":publisher_verified and expected is not None}
    temporary=path.with_suffix(path.suffix+".part")
    count=0
    digest=hashlib.sha512()
    with urllib.request.urlopen(url,timeout=60) as response,temporary.open("wb") as target:
        while chunk:=response.read(1024*1024):
            count+=len(chunk)
            if count>1024**3:raise ValueError("Asset exceeds 1 GiB budget")
            digest.update(chunk)
            target.write(chunk)
    actual=digest.hexdigest()
    if expected and actual!=expected:raise ValueError("Publisher asset digest mismatch: "+name)
    temporary.replace(path)
    print("downloaded "+name+" "+str(count),flush=True)
    return {"url":url,"file":path.relative_to(ROOT).as_posix(),"sha512":actual,"bytes":count,"expected_digest_verified":expected is not None,"publisher_digest_verified":publisher_verified and expected is not None}


def main():
    DIRECTORY.mkdir(parents=True,exist_ok=True)
    lock_path=ROOT/"configs/nemo_vm_assets.lock.json"
    if lock_path.exists():
        lock=json.loads(lock_path.read_text(encoding="utf-8"))
        if lock.get("schema_version")!="nemo-vm-assets-lock-v1":raise ValueError("Unsupported VM asset lock")
        jobs=[]
        for row in lock["assets"]:
            name=Path(row["file"]).name
            if row["file"]!=".runtime/nemo-vm/assets/"+name:raise ValueError("Asset path escapes owned directory")
            jobs.append((row["url"],name,row["sha512"],row["publisher_digest_verified"]))
        with ThreadPoolExecutor(max_workers=3) as pool:assets=list(pool.map(lambda row:fetch(*row),jobs))
        result={"purpose":lock["purpose"],"assets":assets,"automatic_system_install":False,"exact_asset_lock":True}
        (DIRECTORY/"manifest.json").write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
        print(json.dumps({"assets_verified":len(assets),"download_policy":"locked_bytes_no_implicit_upgrade"}))
        return 0
    qemu="https://qemu.weilnetz.de/w64/qemu-w64-setup-20260811"
    with urllib.request.urlopen(qemu+".sha512",timeout=30) as response:qemu_digest=response.read().decode().split()[0]
    debian="https://cloud.debian.org/images/cloud/trixie/latest/"
    with urllib.request.urlopen(debian+"SHA512SUMS",timeout=30) as response:
        lines=response.read().decode().splitlines()
    image="debian-13-genericcloud-amd64.qcow2"
    image_digest=next(line.split()[0] for line in lines if line.split()[-1].lstrip("*")==image)
    jobs=[(qemu+".exe","qemu.exe",qemu_digest,True),
          ("https://github.com/ip7z/7zip/releases/download/26.04/7z2604-x64.msi","7zip.msi",None,False),
          (debian+image,image,image_digest,True)]
    with ThreadPoolExecutor(max_workers=3) as pool:assets=list(pool.map(lambda row:fetch(*row),jobs))
    result={"purpose":"real Linux CPU runtime for unmodified NeMo Gym on a Windows host without WSL","assets":assets,"automatic_system_install":False}
    (DIRECTORY/"manifest.json").write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(result,indent=2))
    return 0


if __name__=="__main__":raise SystemExit(main())
