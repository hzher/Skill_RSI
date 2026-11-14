"""Read-only documentation checks; historical and task evidence is never removed."""
from collections import Counter, defaultdict
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ("README.md", "docs/README.md", "docs/execution_plan.md", "docs/architecture.md",
             "docs/overview.md", "docs/current_status.md")
RETIRED_NAVIGATION_TARGETS = {
    "docs/mainline_plan.md": "docs/execution_plan.md",
    "docs/construction_spec.md": "docs/execution_plan.md",
    "docs/mainline_architecture.md": "docs/architecture.md",
    "docs/mainline_readme.md": "README.md",
    "docs/construction_diary.md": "docs/README.md",
}
RETIRED_NAVIGATION = frozenset(RETIRED_NAVIGATION_TARGETS)
DOCUMENT_RELOCATIONS = {
    **RETIRED_NAVIGATION_TARGETS,
    "docs/collaboration.md": "docs/overview.md",
    **{"docs/" + name + ".md": "docs/reference/" + name + ".md" for name in (
        "collection_readiness", "developer_tools", "environment_api", "evidence_review",
        "execution_interfaces", "interactive_protocol", "interruption_design", "local_foundation",
        "runtime_reliability", "task_contract", "tool_contract", "validation_and_resources",
        "coverage_and_splits", "variant_notes")},
    **{"docs/" + name + ".md": "docs/history/" + name + ".md" for name in (
        "acceptance_checks", "adapter_notes", "decision_log", "environment_inventory",
        "handoff", "issues", "setup", "task_inventory")},
    "docs/guides/toolathlon_failure_repair.md": "docs/history/toolathlon_failure_repair.md",
}
MAX_BYTES = 2 * 1024 * 1024
MAP_BEGIN, MAP_END = "<!-- DOCS_FILE_MAP_BEGIN -->", "<!-- DOCS_FILE_MAP_END -->"


def document_reference_path(root, reference):
    """Resolve published documentation moves without rewriting frozen records."""
    return Path(root) / DOCUMENT_RELOCATIONS.get(reference, reference)


def read_local(root, path):
    # Do not follow a link or junction into runtime/upstream/private data.
    if not path.is_relative_to(root):
        raise ValueError("Path leaves project: " + str(path))
    current = path
    while current != root:
        if current.is_symlink() or current.is_junction():
            raise ValueError("Linked documentation entry: " + str(current))
        current = current.parent
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("Missing or oversized file: " + str(path))
    with path.open("rb") as stream:
        value = stream.read(MAX_BYTES + 1)
    if len(value) > MAX_BYTES:
        raise ValueError("File grew beyond budget: " + str(path))
    return value


def markdown_links(text):
    """Check local inline and reference destinations; skip code and URL fragments."""
    lines, fence = [], None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None:
            lines.append(re.sub(r"(`+).*?\1", "", line))
    prose = "\n".join(lines)
    inline = re.findall(r"\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+['\"][^\n]*?['\"])?\s*\)", prose)
    references = re.findall(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]+>|\S+)", prose, re.MULTILINE)
    return [link[1:-1] if link.startswith("<") else link for link in inline + references]


def markdown_anchors(text):
    """GFM heading anchors for the simple headings used by current project docs."""
    anchors, seen, fence = set(), Counter(), None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None: fence = token
            elif token[0] == fence[0] and len(token) >= len(fence): fence = None
            continue
        if fence is not None: continue
        heading = re.match(r"^ {0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$", line)
        if not heading: continue
        title = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", heading[1])
        title = re.sub(r"<[^>]+>", "", title)
        base = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        suffix = "" if seen[base] == 0 else "-" + str(seen[base])
        anchors.add(base + suffix)
        seen[base] += 1
    return anchors


def file_map_errors(root, text, files):
    if text.count(MAP_BEGIN) != 1 or text.count(MAP_END) != 1 or text.index(MAP_BEGIN) >= text.index(MAP_END):
        return [{"kind": "missing_or_ambiguous_file_map"}]
    body = text.split(MAP_BEGIN, 1)[1].split(MAP_END, 1)[0]
    listed = Counter()
    for link in markdown_links(body):
        parsed = urlsplit(link)
        if parsed.scheme or parsed.netloc or not parsed.path: continue
        target = (root / "docs" / unquote(parsed.path)).resolve()
        if target.is_relative_to(root / "docs"):
            listed[target.relative_to(root).as_posix()] += 1
    errors = []
    if set(listed) != set(files):
        errors.append({"kind": "file_map_coverage", "missing": sorted(set(files) - set(listed)),
                       "extra": sorted(set(listed) - set(files))})
    if any(count != 1 for count in listed.values()):
        errors.append({"kind": "duplicate_file_map_entry", "paths": sorted(n for n, count in listed.items() if count != 1)})
    return errors


def inspect_project(root):
    root = Path(root).resolve()
    errors, historical_links, hashes = [], [], {}
    groups = defaultdict(list)
    docs = root / "docs"
    pending, paths, entries, doc_files = [docs], [root / "README.md"], 0, []
    while pending:
        directory = pending.pop()
        if directory.is_symlink() or directory.is_junction():
            raise ValueError("Linked documentation directory")
        for path in sorted(directory.iterdir()):
            entries += 1
            if entries > 1000:
                raise ValueError("Documentation inventory exceeds budget")
            if path.is_symlink() or path.is_junction():
                raise ValueError("Linked documentation entry")
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                doc_files.append(path.relative_to(root).as_posix())
                if path.suffix == ".md": paths.append(path)
    texts = {}
    for path in sorted(paths):
        name = path.relative_to(root).as_posix()
        raw = read_local(root, path)
        hashes[name] = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8-sig")
        texts[name] = text
        # Normalise CRLF so Git checkout settings do not hide full mirrors.
        if text.strip():
            groups[hashlib.sha256(text.replace("\r\n", "\n").encode()).hexdigest()].append(name)
    duplicates = [names for names in groups.values() if len(names) > 1]
    for names in duplicates:
        errors.append({"kind": "duplicate_document", "paths": names})
    for name in CANONICAL:
        if name not in texts:
            errors.append({"kind": "missing_current_document", "path": name})
    for name in sorted(RETIRED_NAVIGATION.intersection(texts)):
        errors.append({"kind": "retired_navigation_restored", "path": name})
    errors.extend(file_map_errors(root, texts.get("docs/README.md", ""), doc_files))
    for name, text in texts.items():
        for link in markdown_links(text):
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc:
                continue
            target = (root / name).parent / unquote(parsed.path) if parsed.path else root / name
            resolved = target.resolve()
            issue = None
            if not resolved.is_relative_to(root):
                issue = {"kind": "link_leaves_project", "path": name, "link": link}
            elif not resolved.exists():
                issue = {"kind": "missing_link_target", "path": name, "link": link}
            elif parsed.fragment:
                target_name = resolved.relative_to(root).as_posix()
                if target_name in texts and unquote(parsed.fragment) not in markdown_anchors(texts[target_name]):
                    issue = {"kind": "missing_section_anchor", "path": name, "link": link}
            if issue:
                active = name in CANONICAL or name.startswith(("docs/guides/", "docs/reference/"))
                (errors if active else historical_links).append(issue)
    status_paths = (root / "docs/mainline_status.json", root / "evidence/mainline/progress.json")
    if status_paths[0].exists() and status_paths[1].exists():
        values = [read_local(root, path) for path in status_paths]
        for path, raw in zip(status_paths, values):
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(raw).hexdigest()
        same = json.loads(values[0]) == json.loads(values[1])
        if not same:
            errors.append({"kind": "generated_status_mismatch"})
        status = "equal" if same else "different"
    elif any(path.exists() for path in status_paths):
        errors.append({"kind": "missing_generated_status_copy"})
        status = "missing_copy"
    else:
        status = "not_present_in_source_only_checkout"
    suites = defaultdict(list)
    for index, path in enumerate(sorted((root / "configs/suites").glob("*.json"))):
        if index >= 256:
            raise ValueError("Suite inventory exceeds budget")
        raw = read_local(root, path)
        suites[hashlib.sha256(raw).hexdigest()].append((path.relative_to(root).as_posix(), len(raw)))
    repeated = [{"sha256": key, "paths": [p for p, _ in rows],
                 "copies": len(rows), "duplicate_bytes": sum(size for _, size in rows[1:])}
                for key, rows in suites.items() if len(rows) > 1]
    return {"schema_version": "project-hygiene-v1", "passed": not errors,
            "documents_checked": len(texts), "mapped_document_files": len(doc_files), "duplicate_documents": duplicates, "errors": errors,
            "historical_link_warnings": historical_links, "generated_status": status,
            "repeated_suite_groups": repeated,
            "suite_duplicate_bytes": sum(item["duplicate_bytes"] for item in repeated),
            "input_sha256": hashes,
            "scope": "Local docs and suite inventory; no semantic audit, execution, deletion or background schedule"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Include full hashes and historical link warnings")
    args = parser.parse_args(argv)
    try:
        result = inspect_project(ROOT)
    except (OSError, ValueError, UnicodeError) as exc:
        result = {"passed": False, "error": str(exc)}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Project hygiene:", "PASS" if result["passed"] else "FAIL")
        if "error" in result:
            print(result["error"])
        else:
            print("Documents:", result["documents_checked"], "| Duplicate groups:", len(result["duplicate_documents"]),
                  "| Historical link warnings:", len(result["historical_link_warnings"]))
            print("Generated status:", result["generated_status"])
            print("Repeated suites:", sum(item["copies"] for item in result["repeated_suite_groups"]),
                  "files in", len(result["repeated_suite_groups"]), "groups | Duplicate bytes:", result["suite_duplicate_bytes"], "(retained)")
            for error in result["errors"]:
                print(json.dumps(error, ensure_ascii=False))
            print("Use --json for full details. No files were changed.")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
