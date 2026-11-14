"""Validate and extract bounded source bundles without platform path aliases."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

from workenv.core import contained

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_CONTENT_BYTES = 30 * 1024 * 1024
RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"
}


def validate_member(name):
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name
        or any(ord(c) < 32 for c in name) or PurePosixPath(name).is_absolute()
        or PurePosixPath(name).as_posix() != name):
        raise ValueError("Unsafe archive member")
    for part in name.split("/"):
        if part in ("", ".", "..") or part.endswith((" ", ".")) or part.split(".")[0].upper() in RESERVED:
            raise ValueError("Unsafe archive member component")


def extract_bundle(bundle, target):
    """Validate the complete archive before creating the new owned destination."""
    bundle, target = Path(bundle), Path(target)
    if bundle.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("Unexpected compressed archive size")
    data = bundle.read_bytes()
    checksum = hashlib.sha256(data).hexdigest()
    declared = bundle.with_suffix(".zip.sha256").read_text(encoding="ascii").strip()
    if checksum != declared:
        raise ValueError("Bundle SHA256 mismatch")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if not entries or len(entries) > 2000 or sum(e.file_size for e in entries) > MAX_CONTENT_BYTES:
            raise ValueError("Unexpected review archive size")
        names = [e.filename for e in entries]
        folded = {name.casefold() for name in names}
        if len(folded) != len(names):
            raise ValueError("Duplicate or case-aliased archive names")
        for entry in entries:
            validate_member(entry.filename)
            kind = (entry.external_attr >> 16) & 0o170000
            if entry.is_dir() or kind not in (0, 0o100000) or entry.flag_bits & 1:
                raise ValueError("Only regular unencrypted archive files are supported")
            parents = PurePosixPath(entry.filename).parents
            if any(parent.as_posix().casefold() in folded for parent in parents if parent.as_posix() != "."):
                raise ValueError("Archive file/directory prefix collision")
        manifest = json.loads(archive.read("BUNDLE_MANIFEST.json"))
        if not isinstance(manifest, dict):
            raise ValueError("Unsupported source manifest")
        files = manifest.get("files")
        if manifest.get("format") != "office-environment-review-v1" or not isinstance(files, dict):
            raise ValueError("Unsupported source manifest")
        if set(names) != set(files) | {"BUNDLE_MANIFEST.json"} or "BUNDLE_MANIFEST.json" in files:
            raise ValueError("Archive member mismatch")
        for name, value in files.items():
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("Invalid member hash")
            if hashlib.sha256(archive.read(name)).hexdigest() != value:
                raise ValueError("Member hash mismatch")
        contained(target.parent, target.name)
        target.mkdir(parents=True, exist_ok=False)
        for entry in entries:
            destination = contained(target, entry.filename)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(archive.read(entry.filename))
    return {"bundle_sha256": checksum, "checked_members": len(files)}
