"""Owned inventory and source-bundle publication against real temporary trees."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from workenv.core import digest
from workenv.handoff import extract_bundle
from workenv.leases import owned_lock
from workenv import source_inventory as inventory


class SourceInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix="source package 环境 ")
        self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name).resolve()
        self.root=self.base/"project"
        self.root.mkdir()
        (self.root/"README.md").write_text("Explicit source fixture",encoding="utf-8")
        (self.root/".gitignore").write_text(".venv/\n",encoding="utf-8")
        for name in inventory.SOURCE_DIRS:(self.root/name).mkdir()
        (self.root/"workenv/示例.py").write_text("VALUE = 1\n",encoding="utf-8")

    def build(self):
        return inventory.build_bundle(self.root)

    def test_manifest_binds_the_exact_packaged_bytes_and_extracts(self):
        result=self.build()
        bundle=self.root/result["bundle"]
        self.assertEqual(digest(bundle),result["sha256"])
        with zipfile.ZipFile(bundle) as archive:
            manifest=json.loads(archive.read("BUNDLE_MANIFEST.json"))
            self.assertEqual(set(archive.namelist()),set(manifest["files"])|{"BUNDLE_MANIFEST.json"})
        extracted=self.base/"extracted new 环境"
        self.assertEqual(extract_bundle(bundle,extracted)["bundle_sha256"],result["sha256"])
        self.assertEqual((extracted/"workenv/示例.py").read_text(encoding="utf-8"),"VALUE = 1\n")

    def test_mainline_package_includes_all_framework_extension_modules(self):
        for name in inventory.MAINLINE_DIRS:
            (self.root/name).mkdir()
            (self.root/name/"component.py").write_text("VALUE = 'mainline'\n",encoding="utf-8")
        result=inventory.build_bundle(self.root,include_mainline=True)
        with zipfile.ZipFile(self.root/result["bundle"]) as archive:
            for name in inventory.MAINLINE_DIRS:self.assertIn(name+"/component.py",archive.namelist())
            self.assertFalse(any(name.startswith((".runtime/",".upstream/",".venv/")) for name in archive.namelist()))

    def test_credential_names_and_caches_are_excluded_consistently(self):
        (self.root/"configs/.env").write_text("SECRET",encoding="utf-8")
        (self.root/"configs/.env.production").write_text("SECRET",encoding="utf-8")
        (self.root/"configs/.env.example").write_text("KEY=",encoding="utf-8")
        (self.root/"configs/.hidden_credential").write_text("SECRET",encoding="utf-8")
        (self.root/"tests/__pycache__").mkdir()
        (self.root/"tests/__pycache__/cache.pyc").write_bytes(b"CACHE")
        (self.root/"reports/.private_cache").mkdir()
        (self.root/"reports/.private_cache/private").write_bytes(b"SECRET")
        files=inventory.snapshot(self.root)
        self.assertIn("configs/.env.example",files)
        self.assertFalse(any(name.endswith((".env",".env.production",".hidden_credential",".pyc")) or ".private_cache" in name for name in files))

    def test_linked_directory_is_refused_without_following_outside(self):
        outside=self.base/"outside"
        outside.mkdir()
        sentinel=outside/"secret.txt"
        sentinel.write_bytes(b"OUTSIDE")
        link=self.root/"docs/linked"
        if os.name=="nt":
            completed=subprocess.run(["cmd","/c","mklink","/J",str(link),str(outside)],capture_output=True)
            self.assertEqual(completed.returncode,0)
            self.addCleanup(link.rmdir)
        else:
            link.symlink_to(outside,target_is_directory=True)
            self.addCleanup(link.unlink)
        with self.assertRaises(ValueError):inventory.snapshot(self.root)
        self.assertEqual(sentinel.read_bytes(),b"OUTSIDE")

    def test_linked_file_is_refused_when_local_symlink_creation_is_available(self):
        external=self.base/"outside.txt"
        external.write_bytes(b"OUTSIDE")
        link=self.root/"reports/linked.txt"
        try:link.symlink_to(external)
        except OSError as exc:self.skipTest("Local file symlink creation unavailable: "+type(exc).__name__)
        self.addCleanup(link.unlink)
        with self.assertRaises(ValueError):inventory.snapshot(self.root)

    def test_real_hardlinked_file_is_refused_without_reading_its_body(self):
        outside=self.base/"outside_hardlink.txt"
        outside.write_bytes(b"OUTSIDE_HARDLINK")
        link=self.root/"reports/hardlinked.txt"
        link.hardlink_to(outside)
        self.addCleanup(link.unlink)
        self.assertGreater(link.stat().st_nlink,1)
        with self.assertRaises(ValueError):inventory.snapshot(self.root)
        self.assertEqual(outside.read_bytes(),b"OUTSIDE_HARDLINK")

    def test_collection_limit_failure_preserves_the_last_bundle_and_checksum(self):
        result=self.build()
        bundle=self.root/result["bundle"]
        before=bundle.read_bytes()
        checksum=bundle.with_suffix(".zip.sha256").read_bytes()
        with patch.object(inventory,"MAX_MEMBERS",2):
            with self.assertRaises(ValueError):self.build()
        self.assertEqual(before,bundle.read_bytes())
        self.assertEqual(checksum,bundle.with_suffix(".zip.sha256").read_bytes())
        self.assertFalse((bundle.parent/".package.lock").exists())

    def test_source_change_during_zip_creation_is_rejected_before_publication(self):
        result=self.build()
        bundle=self.root/result["bundle"]
        before=bundle.read_bytes()
        original=zipfile.ZipFile.writestr
        changed=False
        def mutate(archive,name,data,*args,**kwargs):
            nonlocal changed
            returned=original(archive,name,data,*args,**kwargs)
            if not changed:
                changed=True
                (self.root/"README.md").write_text("Changed during package",encoding="utf-8")
            return returned
        with patch.object(zipfile.ZipFile,"writestr",mutate):
            with self.assertRaisesRegex(ValueError,"changed while packaging"):self.build()
        self.assertEqual(before,bundle.read_bytes())
        self.assertEqual(list(bundle.parent.glob("*.tmp")),[])

    def test_existing_packager_lock_refuses_another_builder(self):
        directory=self.root/"evidence/handoff"
        directory.mkdir(parents=True)
        with owned_lock(directory/".package.lock",self.root,"explicit builder fixture"):
            with self.assertRaises(FileExistsError):self.build()

    def test_byte_budget_rejects_large_source_before_publication(self):
        with patch.object(inventory,"MAX_CONTENT_BYTES",8):
            with self.assertRaises(ValueError):self.build()
        self.assertFalse((self.root/"evidence/handoff/local_review_bundle.zip").exists())

    def test_output_aliases_devices_and_traversal_are_rejected(self):
        for destination in ("../escape.zip","evidence/handoff/CON.zip","evidence/handoff/a:stream.zip","reports/outside.zip"):
            with self.assertRaises(ValueError):inventory.build_bundle(self.root,destination=destination)
        self.assertFalse((self.root/"evidence").exists())
