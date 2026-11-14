"""Original MCP compiled inputs are checked before a diagnostic child can start."""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
import json
from unittest.mock import patch

from integrations.howtocook_component import verify_component, recover_component_lock
from workenv.leases import owned_lock


class HowToCookComponentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.base = self.root / ".runtime/howtocook-build/unit"
        (self.base / "build").mkdir(parents=True)
        (self.base / "build/index.js").write_bytes(b"// original compiled fixture")
        source = self.root / "configs/fixture.json"
        source.parent.mkdir()
        source.write_bytes(b"{}")
        self.receipt = {"schema_version": "howtocook-offline-build-v1", "passed": True, "typecheck_completed": True, "compilation_mode": "full_typecheck",
            "build_ref": self.base.relative_to(self.root).as_posix(),
            "source_sha256": {"configs/fixture.json": hashlib.sha256(source.read_bytes()).hexdigest()},
            "compiled_sha256": {"build/index.js": hashlib.sha256((self.base / "build/index.js").read_bytes()).hexdigest()}}

    def test_matching_compiled_component_is_resolved_without_execution(self):
        self.assertEqual(verify_component(self.root, self.receipt), self.base)

    def test_replaced_or_extra_compiled_modules_are_rejected(self):
        path = self.base / "build/index.js"
        original = path.read_bytes()
        path.write_bytes(b"x" * len(original))
        with self.assertRaises(ValueError): verify_component(self.root, self.receipt)
        path.write_bytes(original)
        (self.base / "build/extra.js").write_bytes(b"// not in manifest")
        with self.assertRaises(ValueError): verify_component(self.root, self.receipt)

    def test_source_edits_and_foreign_build_roots_are_rejected(self):
        other = deepcopy(self.receipt)
        other["build_ref"] = "configs"
        with self.assertRaises(ValueError): verify_component(self.root, other)
        (self.root / "configs/fixture.json").write_bytes(b'{"changed":true}')
        with self.assertRaises(ValueError): verify_component(self.root, self.receipt)

    def test_compilation_mode_and_typecheck_claim_must_be_consistent(self):
        for changed in ({"passed": False}, {"typecheck_completed": False}, {"compilation_mode": "unknown"}):
            with self.assertRaises(ValueError): verify_component(self.root, {**self.receipt, **changed})
        self.assertEqual(verify_component(self.root, {**self.receipt, "typecheck_completed": False, "compilation_mode": "bounded_emit_only"}), self.base)

    def test_live_or_unknown_owner_lock_is_preserved(self):
        path = self.base / ".component.lock"
        with owned_lock(path, self.root, "howtocook_component"):
            original = path.read_bytes()
            for status in ("alive", "unknown"):
                with patch("integrations.howtocook_component.process_identity", return_value={"status": status}):
                    with self.assertRaises(ValueError): recover_component_lock(self.root, self.base)
                self.assertEqual(path.read_bytes(), original)

    def test_dead_owner_lock_is_archived_without_discarding_original_bytes(self):
        path = self.base / ".component.lock"
        record = {"schema_version": "workenv-lock-v0.1", "kind": "howtocook_component", "workspace_owner": str(self.root),
                  "resource": path.relative_to(self.root).as_posix(), "process": {"pid": 123456}}
        original = json.dumps(record).encode()
        path.write_bytes(original)
        with patch("integrations.howtocook_component.process_identity", return_value={"status": "not_found"}):
            archived = recover_component_lock(self.root, self.base)
        self.assertFalse(path.exists())
        self.assertEqual((self.root / archived).read_bytes(), original)


class RecipeDependencyTests(unittest.TestCase):
    def setUp(self):
        self.package = {"dependencies": {"zod": "3.25.76", "@modelcontextprotocol/sdk": "1.13.0"}, "devDependencies": {"typescript": "5.9.3"}}
        self.lock = {"lockfileVersion": 3, "packages": {"": deepcopy(self.package), "node_modules/zod": {"version": "3.25.76"}}}

    def test_single_exact_zod_version_matches_both_manifest_and_lock(self):
        from scripts.build_howtocook_offline import validate_dependency_lock
        self.assertEqual(validate_dependency_lock(self.package, self.lock), {"zod_version": "3.25.76", "zod_instances": 1})

    def test_nested_zod_copy_is_rejected_even_if_its_version_matches(self):
        from scripts.build_howtocook_offline import validate_dependency_lock
        self.lock["packages"]["node_modules/@modelcontextprotocol/sdk/node_modules/zod"] = {"version": "3.25.76"}
        with self.assertRaises(ValueError): validate_dependency_lock(self.package, self.lock)

    def test_different_installed_version_stale_lock_and_unpinned_manifest_are_rejected(self):
        from scripts.build_howtocook_offline import validate_dependency_lock
        for key, value in [("version", "3.24.1"), ("lock", {}), ("manifest", "^3.25.76")]:
            lock, package = deepcopy(self.lock), deepcopy(self.package)
            if key == "version": lock["packages"]["node_modules/zod"]["version"] = value
            elif key == "lock": lock["packages"][""].update(dependencies=value)
            else: package["dependencies"]["zod"] = value
            with self.assertRaises(ValueError): validate_dependency_lock(package, lock)
