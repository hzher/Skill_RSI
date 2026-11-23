"""Workspace preparation adapted from Skill_RSI_hzh/framework/skillrl/storage.py (MIT)."""
import shutil
from pathlib import Path

from ..evidence.io import digest, inside


def file_sha(path):
    return digest(Path(path).read_bytes())


def contained(root, name):
    return inside(Path(root), name)


def copy_public(source, destination):
    source, destination = Path(source).resolve(strict=True), Path(destination)
    if not source.is_dir():
        raise ValueError("public input must be a directory")
    members = sorted(source.rglob("*"))
    if any(item.is_symlink() or not (item.is_file() or item.is_dir()) for item in members):
        raise ValueError("public input must contain only ordinary files and directories")
    if destination.resolve().is_relative_to(source):
        raise ValueError("workspace must be outside the public input tree")
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    for item in members:
        target = destination / item.relative_to(source)
        if item.is_dir():
            target.mkdir(exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)


def files(root):
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"expected a directory: {root}")
    result = {}
    for item in sorted(root.rglob("*")):
        if item.is_symlink():
            raise ValueError("symlink in workspace artifacts")
        if item.is_file():
            result[item.relative_to(root).as_posix()] = {"sha256": file_sha(item), "bytes": item.stat().st_size}
    return result
