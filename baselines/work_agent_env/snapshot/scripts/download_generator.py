"""Download a pinned SkillOS model; verify weights against upstream LFS SHA256."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import time
import urllib.request
from datetime import datetime, timezone

REPO = "Qwen/Qwen3-8B"
REVISION = "b968826d9c46dd6066d109eabc6255188de91218"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--revision", default=REVISION)
    parser.add_argument("--output", type=Path, default=Path(".runtime/models/Qwen3-8B"))
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--block-mib", type=int, choices=(1, 2, 4, 8, 16), default=16)
    parser.add_argument("--download-endpoint", default="https://huggingface.co")
    parser.add_argument("--direct-download", action="store_true", help="Bypass environment proxy for weight bytes; upstream hashes still verified")
    parser.add_argument("--resolved-direct", action="store_true", help="Resolve one public CDN URL per file, then range-download directly without resolve endpoint rate limits")
    parser.add_argument("--weight-source", choices=("huggingface", "modelscope"), default="huggingface",
                        help="Byte transport only; every shard must still match the pinned Hugging Face LFS hash")
    args = parser.parse_args()
    download = urllib.request.build_opener(urllib.request.ProxyHandler({})) if args.direct_download else urllib.request.build_opener()
    args.output.mkdir(parents=True, exist_ok=True)
    repo, revision = args.repo, args.revision
    url = f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true"
    with urllib.request.urlopen(url, timeout=60) as response:
        info = json.load(response)
    if info["sha"] != revision:
        raise ValueError("Model revision differs from deployment pin")
    identities = {}
    # Tokenizer/config files become usable for protocol checks while weights download.
    for row in sorted(info["siblings"], key=lambda row: (row["rfilename"].endswith(".safetensors"), row["rfilename"])):
        name = row["rfilename"]
        if "/" in name or not (name.endswith((".json", ".safetensors", ".txt", ".model", ".jinja"))
                               or name in ("LICENSE", "README.md")):
            continue
        dest = args.output / name
        if "lfs" not in row:
            if not dest.is_file():
                with urllib.request.urlopen(f"https://huggingface.co/{repo}/resolve/{revision}/{name}", timeout=60) as response:
                    dest.write_bytes(response.read())
        else:
            size, expected = row["lfs"]["size"], row["lfs"]["sha256"]
            if dest.is_file() and hashlib.file_digest(dest.open("rb"), "sha256").hexdigest() == expected:
                identities[name] = {"sha256": expected, "bytes": size}
                continue
            legacy_chunks = args.output / (".download_chunks_" + name)
            chunks = legacy_chunks if args.block_mib == 16 else args.output / (".download_chunks_" + str(args.block_mib) + "MiB_" + name)
            chunks.mkdir(exist_ok=True)
            block = args.block_mib * 1024 * 1024
            count = (size + block - 1) // block
            direct_cdn = None
            if args.resolved_direct:
                for attempt in range(4):
                    if args.weight_source == "modelscope" and attempt < 2:
                        resolve = f"https://modelscope.cn/models/{repo}/resolve/master/{name}"
                        resolver = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    else:
                        endpoint = "https://huggingface.co" if attempt % 2 == 0 else "https://hf-mirror.com"
                        resolver = urllib.request.build_opener() if attempt % 2 == 0 else urllib.request.build_opener(urllib.request.ProxyHandler({}))
                        resolve = f"{endpoint}/{repo}/resolve/{revision}/{name}?download=true"
                    try:
                        with resolver.open(urllib.request.Request(resolve, headers={"Range": "bytes=0-1023"}), timeout=30) as response:
                            direct_cdn = response.geturl()
                            response.read(1024)
                        break
                    except Exception as exc:
                        print(json.dumps({"event": "resolve_retry", "file": name, "attempt": attempt + 1,
                            "error_type": type(exc).__name__, "http_status": getattr(exc, "code", None)}), flush=True)
                        if attempt < 3:
                            time.sleep(attempt + 1)
            print(json.dumps({"downloading": name, "bytes": size, "chunks": count, "workers": args.workers}), flush=True)
            def fetch(index):
                start, end = index * block, min(size, (index + 1) * block) - 1
                part = chunks / f"{index:05d}"
                if part.is_file() and part.stat().st_size == end - start + 1:
                    return part
                # Re-slice complete old 16-MiB chunks instead of downloading them again.
                if args.block_mib != 16:
                    legacy_block = 16 * 1024 * 1024
                    legacy_index = start // legacy_block
                    old = legacy_chunks / f"{legacy_index:05d}"
                    if old.is_file() and old.stat().st_size == min(legacy_block, size - legacy_index * legacy_block):
                        with old.open("rb") as stream:
                            stream.seek(start % legacy_block)
                            data = stream.read(end - start + 1)
                        if len(data) == end - start + 1:
                            partial_chunk = part.with_suffix(".partial")
                            partial_chunk.write_bytes(data)
                            partial_chunk.replace(part)
                            return part
                for attempt in range(4):
                    try:
                        # Unique query avoids a proxy cache reusing a different Range response.
                        endpoint = args.download_endpoint if attempt == 0 else "https://huggingface.co"
                        remote = direct_cdn if direct_cdn and attempt == 0 else f"{endpoint}/{repo}/resolve/{revision}/{name}?download=true&chunk={index}"
                        request = urllib.request.Request(remote, headers={"Range": f"bytes={start}-{end}"})
                        opener = (urllib.request.build_opener(urllib.request.ProxyHandler({})) if direct_cdn else download) if attempt == 0 else urllib.request.build_opener()
                        with opener.open(request, timeout=120) as response:
                            if response.status != 206 or not response.headers.get("Content-Range", "").startswith(f"bytes {start}-{end}/"):
                                raise ValueError("Server did not honor the exact byte range")
                            data = response.read()
                        if len(data) != end - start + 1:
                            raise ValueError("Incomplete model chunk")
                        partial_chunk = part.with_suffix(".partial")
                        partial_chunk.write_bytes(data)
                        partial_chunk.replace(part)
                        return part
                    except Exception as exc:
                        print(json.dumps({"chunk": index, "attempt": attempt + 1, "error_type": type(exc).__name__,
                                          "http_status": getattr(exc, "code", None)}), flush=True)
                        if attempt == 3:
                            failure = {"observed_at": datetime.now(timezone.utc).isoformat(), "repo": repo,
                                "revision": revision, "file": name, "chunk": index, "error_type": type(exc).__name__,
                                "http_status": getattr(exc, "code", None), "status": "download_failed_no_complete_checkpoint"}
                            (chunks / (str(index) + ".failure.json")).write_text(json.dumps(failure, indent=2) + "\n")
                            raise RuntimeError(f"Model chunk {index} failed") from None
                        time.sleep(attempt + 1)
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                parts = []
                for index, part in enumerate(pool.map(fetch, range(count)), 1):
                    parts.append(part)
                    if index % 12 == 0 or index == count:
                        print(json.dumps({"completed_chunks": index, "total_chunks": count}), flush=True)
            partial = dest.with_suffix(dest.suffix + ".partial")
            with partial.open("wb") as output:
                for part in parts:
                    with part.open("rb") as stream:
                        shutil.copyfileobj(stream, output)
            with partial.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != expected or partial.stat().st_size != size:
                raise ValueError("Downloaded model does not match upstream LFS identity")
            partial.replace(dest)
            shutil.rmtree(chunks)
            if chunks != legacy_chunks and legacy_chunks.is_dir():
                shutil.rmtree(legacy_chunks)
        with dest.open("rb") as stream:
            identities[name] = {"sha256": hashlib.file_digest(stream, "sha256").hexdigest(), "bytes": dest.stat().st_size}
    receipt = {"repo": repo, "revision": revision, "files": identities, "weights_lfs_verified": True,
               "weight_transport": args.weight_source, "identity_authority": "pinned_huggingface_LFS_SHA256"}
    receipt_partial = args.output / "deployment_source.json.partial"
    receipt_partial.write_text(json.dumps(receipt, indent=2) + "\n")
    receipt_partial.replace(args.output / "deployment_source.json")
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
