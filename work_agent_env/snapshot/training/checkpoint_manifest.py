"""Freeze checkpoint file bytes without loading or executing a model."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from workenv.core import no_links

ALLOWED = {".safetensors", ".bin", ".json", ".model", ".txt"}


def inventory(model_root: Path):
    model_root = model_root.absolute()
    no_links(model_root, model_root)
    model_root = model_root.resolve(strict=True)
    if not model_root.is_dir(): raise ValueError("Checkpoint root must be a directory")
    files, pending, visited, total = [], [model_root], 0, 0
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            visited += 1
            if visited > 1024: raise ValueError("Checkpoint inventory budget exceeded")
            no_links(path, model_root)
            if path.is_dir():
                if not path.name.startswith("."): pending.append(path)
            elif path.is_file() and path.suffix in ALLOWED and not path.name.startswith("."):
                size = path.stat().st_size
                total += size
                if len(files) >= 512 or total > 128 * 1024**3: raise ValueError("Checkpoint byte/file budget exceeded")
                before = path.stat()
                with path.open("rb") as source: checksum = hashlib.file_digest(source, "sha256").hexdigest()
                after = path.stat()
                if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
                    raise ValueError("Checkpoint changed while freezing")
                files.append({"path": path.relative_to(model_root).as_posix(), "bytes": size, "sha256": checksum})
    files.sort(key=lambda row: row["path"])
    if not any(Path(row["path"]).suffix in (".bin", ".safetensors") for row in files):
        raise ValueError("No checkpoint weight files found")
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return files, hashlib.sha256(encoded).hexdigest()


def freeze(model_root: Path, registry_path: Path, fixed_executor_id: str):
    if not isinstance(fixed_executor_id, str) or not fixed_executor_id.strip():
        raise ValueError("A frozen Executor identity is required")
    files, model_sha = inventory(model_root)
    return {"schema_version": "workagent-frozen-checkpoint-v1", "model_root": str(model_root.resolve()),
            "model_sha256": model_sha, "files": files, "fixed_executor_id": fixed_executor_id,
            "training_registry_sha256": hashlib.sha256(registry_path.read_bytes()).hexdigest(),
            "validation_scope": "file_bytes_only_not_model_loading_or_training_verification"}


def verify(descriptor, registry_sha256):
    if descriptor.get("schema_version") != "workagent-frozen-checkpoint-v1" or descriptor.get("training_registry_sha256") != registry_sha256:
        raise ValueError("Checkpoint descriptor or training lineage mismatch")
    if not descriptor.get("fixed_executor_id") or not re.fullmatch(r"[0-9a-f]{64}", descriptor.get("model_sha256", "")):
        raise ValueError("Missing frozen model/Executor identity")
    actual, model_sha = inventory(Path(descriptor["model_root"]))
    if actual != descriptor.get("files") or model_sha != descriptor["model_sha256"]:
        raise ValueError("Checkpoint bytes changed after freezing")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--training-registry", type=Path, required=True)
    parser.add_argument("--fixed-executor-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    descriptor = freeze(args.model_root, args.training_registry, args.fixed_executor_id)
    with args.output.open("x", encoding="utf-8") as target:
        json.dump(descriptor, target, indent=2)
        target.write("\n")
    print(json.dumps({"descriptor": str(args.output), "files": len(descriptor["files"]), "validation_scope": descriptor["validation_scope"]}))
    return 0


if __name__ == "__main__": raise SystemExit(main())
