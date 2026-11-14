"""Build the pinned original HowToCook MCP in an owned offline-data directory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from training.recipe_snapshot import verify_snapshot
from workenv.core import contained, no_links, save
from workenv.leases import owned_lock

PREFIX = "local_servers/HowToCook-mcp/"
PATCH = '''import fs from 'node:fs';
import ts from 'typescript';
const path=process.argv[2];
const text=fs.readFileSync(path,'utf8');
const tree=ts.createSourceFile(path,text,ts.ScriptTarget.Latest,true);
const found=tree.statements.filter(node=>ts.isFunctionDeclaration(node)&&node.name?.text==='fetchRecipes');
if(found.length!==1||!found[0].body) throw new Error('Original loader function changed');
const imports=tree.statements.filter(node=>ts.isImportDeclaration(node)&&node.moduleSpecifier.text==='./all_recipes.json');
if(imports.length!==1||imports[0].importClause?.name?.text!=='localRecipes') throw new Error('Original JSON data import changed');
const body=found[0].body;
const replacement='{ return localRecipes as Recipe[]; }';
const dataImport="import { readFileSync } from 'node:fs';\\nconst localRecipes: Recipe[] = JSON.parse(readFileSync(new URL('./all_recipes.json', import.meta.url), 'utf8'));";
const edits=[{start:body.getStart(tree),end:body.end,value:replacement},
             {start:imports[0].getStart(tree),end:imports[0].end,value:dataImport}].sort((a,b)=>b.start-a.start);
let result=text;for(const edit of edits) result=result.slice(0,edit.start)+edit.value+result.slice(edit.end);
fs.writeFileSync(path,result);
'''


def source_inputs(root):
    source = json.loads((root / "configs/mainline_sources.lock.json").read_text(encoding="utf-8"))["sources"]["toolathlon_gym"]
    checkout = contained(root, source["checkout"])
    if not re.fullmatch(r"[0-9a-f]{40}", source["commit"]): raise ValueError("Invalid original-service source pin")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(checkout), *args], timeout=30,
                                       stderr=subprocess.PIPE, env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})
    if git("rev-parse", "HEAD").decode().strip() != source["commit"]: raise ValueError("Original source HEAD changed")
    entries = git("ls-tree", "-r", "-z", source["commit"], "--", PREFIX + "src", PREFIX + "tsconfig.json")
    if len(entries) > 65536: raise ValueError("Original source tree metadata exceeds budget")
    files, objects, total = {}, {}, 0
    for entry in entries.split(b"\0"):
        if not entry: continue
        header, name = entry.decode().split("\t", 1)
        mode, kind, oid = header.split()
        if not name.startswith(PREFIX) or mode != "100644" or kind != "blob" or not re.fullmatch(r"[0-9a-f]{40}", oid):
            raise ValueError("Unexpected source member in original MCP tree")
        relative = name[len(PREFIX):]
        if ".." in relative.split("/") or Path(relative).suffix not in (".ts", ".json"): raise ValueError("Unsupported original MCP source")
        size = int(git("cat-file", "-s", oid))
        if not 0 < size <= 2 * 1024 * 1024: raise ValueError("Original MCP source exceeds file budget")
        total += size
        if total > 8 * 1024 * 1024 or len(files) >= 64: raise ValueError("Original MCP source exceeds inventory budget")
        raw = git("cat-file", "blob", oid)
        if len(raw) != size or hashlib.sha1(b"blob " + str(size).encode() + b"\0" + raw).hexdigest() != oid:
            raise ValueError("Original MCP blob identity changed")
        files[relative], objects[relative] = raw, oid
    if not {"src/index.ts", "src/data/recipes.ts", "src/data/all_recipes.json", "tsconfig.json"} <= set(files):
        raise ValueError("Original MCP source inventory incomplete")
    return source["commit"], files, objects


def validate_dependency_lock(package, lock):
    """Avoid cross-version Zod schema expansion at original SDK tool overloads."""
    entries = lock.get("packages", {})
    version = package.get("dependencies", {}).get("zod")
    zod = [name for name in entries if name.endswith("/zod")]
    if (lock.get("lockfileVersion") != 3 or not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
        or zod != ["node_modules/zod"] or entries["node_modules/zod"].get("version") != version
        or any(entries.get("", {}).get(key, {}) != package.get(key, {}) for key in ("dependencies", "devDependencies"))):
        raise ValueError("Original recipe MCP requires one exact, shared Zod version and a matching npm lock")
    return {"zod_version": version, "zod_instances": 1}


def fingerprint(root, commit, objects, recipe_sha256, compilation_mode):
    hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in
              ("scripts/build_howtocook_offline.py", "configs/howtocook.package.json", "configs/howtocook.package-lock.json",
               "configs/mainline_sources.lock.json", "training/recipe_snapshot.py", "training/task_lifecycle_design.py")}
    value = {"source_commit": commit, "source_objects": objects, "recipe_sha256": recipe_sha256,
             "compilation_mode": compilation_mode, "source_sha256": hashes}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest(), value


def build(root=ROOT, *, install=False, emit_only=False):
    root = Path(root).resolve()
    dependency_identity = validate_dependency_lock(
        json.loads(contained(root, "configs/howtocook.package.json").read_text(encoding="utf-8")),
        json.loads(contained(root, "configs/howtocook.package-lock.json").read_text(encoding="utf-8")))
    commit, sources, objects = source_inputs(root)
    snapshot = json.loads((root / "evidence/mainline/howtocook_recipe_snapshot.json").read_text(encoding="utf-8"))
    summary = verify_snapshot(root, snapshot)
    if hashlib.sha256(sources["src/data/all_recipes.json"]).hexdigest() != summary["sha256"]:
        raise ValueError("Original MCP recipe bytes differ from the frozen public snapshot")
    mode = "bounded_emit_only" if emit_only else "full_typecheck"
    digest, identity = fingerprint(root, commit, objects, summary["sha256"], mode)
    base = contained(root, ".runtime/howtocook-build/" + digest[:24])
    if not base.resolve().is_relative_to(root / ".runtime/howtocook-build"): raise ValueError("Build escaped its owned workspace")
    no_links(base, root)
    base.mkdir(parents=True, exist_ok=True)
    with owned_lock(base / ".component.lock", root, "howtocook_component"):
        marker = base / "build_owner.json"
        if marker.exists() and json.loads(marker.read_text()) != identity: raise ValueError("Build directory identity changed")
        save(marker, identity)
        prior_path = base / "build_output.json"
        previous = json.loads(prior_path.read_text()) if prior_path.exists() else None
        for name, raw in {**sources, "package.json": (root / "configs/howtocook.package.json").read_bytes(),
                          "package-lock.json": (root / "configs/howtocook.package-lock.json").read_bytes()}.items():
            path = contained(base, name)
            no_links(path, root)
            # Patched source is verified below; never overwrite a previous compilation.
            if path.exists():
                existing = path.read_bytes()
                if existing == raw: continue
                if (name == "src/data/recipes.ts" and previous and
                    previous.get("generated_source_sha256", {}).get(name) == hashlib.sha256(existing).hexdigest()):
                    path.write_bytes(raw)
                    continue
                raise ValueError("Existing generated source/configuration changed; retain this build directory")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream: stream.write(raw)
        node = shutil.which("node")
        if not node: raise RuntimeError("Node is required for the original MCP service")
        node = str(Path(node).resolve())
        if install:
            npm = shutil.which("npm.cmd" if sys.platform == "win32" else "npm")
            if not npm: raise RuntimeError("npm is required for the explicit dependency install")
            subprocess.run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--prefix", str(base),
                            "--registry", "https://registry.npmjs.org", "--fetch-timeout=30000", "--fetch-retries=1"], check=True, timeout=180)
        if not (base / "node_modules/typescript/lib/tsc.js").is_file(): raise RuntimeError("Install the locked Node dependencies with --install")
        patch = base / "patch_loader.mjs"
        patch.write_text(PATCH, encoding="utf-8")
        subprocess.run([node, str(patch), str(base / "src/data/recipes.ts")], cwd=base, check=True, timeout=20)
        compiler = [node, str(base / "node_modules/typescript/lib/tsc.js"), "--project", str(base / "tsconfig.json"),
                    "--resolveJsonModule", "false", "--extendedDiagnostics"]
        if emit_only: compiler.append("--noCheck")
        subprocess.run(compiler, cwd=base, check=True, timeout=45 if emit_only else 120)
        data_path = base / "build/data/all_recipes.json"
        no_links(data_path, root)
        data_path.parent.mkdir(parents=True, exist_ok=True)
        if data_path.exists() and data_path.read_bytes() != sources["src/data/all_recipes.json"]:
            raise ValueError("Compiled data asset changed")
        data_path.write_bytes(sources["src/data/all_recipes.json"])
        compiled = {p.relative_to(base).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in (base / "build").rglob("*") if p.is_file()}
        if not compiled or len(compiled) > 64: raise ValueError("Unexpected compiled original MCP inventory")
        observed = {name: hashlib.sha256((base / name).read_bytes()).hexdigest() for name in sources}
        if any(observed[name] != hashlib.sha256(raw).hexdigest() for name, raw in sources.items() if name != "src/data/recipes.ts"):
            raise ValueError("Build modified original source outside the allowlisted data-loader function")
        result = {"schema_version": "howtocook-offline-build-v1", "passed": True, **identity, "build_fingerprint": digest,
                "build_ref": base.relative_to(root).as_posix(), "generated_source_sha256": observed,
                "compiled_sha256": compiled, "typecheck_completed": not emit_only, "dependency_identity": dependency_identity,
                "patch_scope": "public recipe JSON import and fetchRecipes body only; original tracked files untouched",
                "runtime_activation": False, "original_mcp_diagnostic_pending": True, "real_model_calls": 0, "training_updates": 0}
        save(prior_path, result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Explicitly install from the fixed npm lock in the owned build directory")
    parser.add_argument("--emit-only", action="store_true", help="Emit JavaScript for isolated MCP diagnostics; semantic typecheck remains unverified")
    args = parser.parse_args()
    try:
        result = build(install=args.install, emit_only=args.emit_only)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        result = {"schema_version": "howtocook-offline-build-v1", "passed": False, "error_type": type(exc).__name__, "error": str(exc)[:500]}
    save(ROOT / "evidence/mainline/howtocook_build.json", result)
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
