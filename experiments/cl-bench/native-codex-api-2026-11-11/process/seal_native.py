"""Copy final native outputs after the benchmark process exits; no model calls."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--archive", required=True)
    args = parser.parse_args()
    runtime = Path(args.runtime).resolve()
    archive = Path(args.archive).resolve()
    config = json.loads((ROOT / "config/project.json").read_text(encoding="utf-8-sig"))
    if runtime.parent != Path(config["runtime"]["work_root"]).resolve() or not archive.is_relative_to(ROOT / "runs"):
        raise RuntimeError("Unexpected output roots")
    if not (archive / "manifest.json").is_file():
        raise RuntimeError("No attempt manifest at archive location")
    records = []
    unchanged = 0
    for current, directories, names in os.walk(runtime, followlinks=False):
        current = Path(current)
        directories[:] = [d for d in directories if d != "data" and not (current / d).is_symlink()]
        for name in names:
            source = current / name
            if source.is_symlink() or not source.is_file():
                continue
            relative = source.relative_to(runtime)
            target = archive / "native" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            existing = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
            if existing == expected:
                unchanged += 1
                records.append({"path": relative.as_posix(), "sha256": expected})
                continue
            if existing is not None:
                preserved = archive / "audit/pre-exit-copy" / relative
                preserved.parent.mkdir(parents=True, exist_ok=True)
                if not preserved.exists():
                    shutil.copy2(target, preserved)
            shutil.copy2(source, target)
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise RuntimeError("Native copy checksum mismatch")
            records.append({"path": relative.as_posix(), "sha256": expected})
    report = {"source_runtime": str(runtime), "files": records, "compared_native_files": len(records), "all_match": True, "unchanged_native_files_verified_without_recopy": unchanged, "copied_after_benchmark_process_exit": True, "model_calls": 0}
    (archive / "control/post-exit-copy.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"native_files_copied_and_verified": len(records)}))


if __name__ == "__main__":
    main()
