"""Regressions for documentation links, navigation and status consistency."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.check_project_hygiene import (
    CANONICAL, MAP_BEGIN, MAP_END, RETIRED_NAVIGATION,
    document_reference_path, inspect_project,
)

ROOT = Path(__file__).resolve().parents[1]


class ProjectHygieneTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in CANONICAL:
            self.write(name, "# Unique " + name + "\n")
        # Keep the historical command section in the index fixture.
        path = self.root / "docs/README.md"
        path.write_text(path.read_text() + "\n## 施工日记和原始命令\n", encoding="utf-8")
        self.refresh_map()

    def refresh_map(self):
        path = self.root / "docs/README.md"
        prefix = path.read_text(encoding="utf-8").split(MAP_BEGIN, 1)[0]
        files = sorted(p.relative_to(path.parent).as_posix() for p in path.parent.rglob("*") if p.is_file())
        path.write_text(prefix + MAP_BEGIN + "\n" + "\n".join("[" + n + "](<" + n + ">)" for n in files) + "\n" + MAP_END + "\n", encoding="utf-8")

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
        index = self.root / "docs/README.md"
        if index.is_file() and MAP_BEGIN in index.read_text(encoding="utf-8"):
            self.refresh_map()

    def test_source_only_check_reports_suites_without_writes(self):
        self.write("configs/suites/a.json", '{"seed":5}\n')
        self.write("configs/suites/b.json", '{"seed":5}\n')
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = inspect_project(self.root)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["repeated_suite_groups"][0]["copies"], 2)
        self.assertEqual(result["generated_status"], "not_present_in_source_only_checkout")
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_full_mirrors_rejected_despite_checkout_line_endings(self):
        self.write("docs/copy_a.md", "# Duplicated\r\n\r\nBody\r\n")
        self.write("docs/copy_b.md", "# Duplicated\n\nBody\n")
        result = inspect_project(self.root)
        self.assertFalse(result["passed"])
        self.assertIn(["docs/copy_a.md", "docs/copy_b.md"], result["duplicate_documents"])

    def test_retired_navigation_is_rejected_even_when_target_is_valid(self):
        for name in sorted(RETIRED_NAVIGATION):
            with self.subTest(path=name):
                self.write(name, "[Plan](execution_plan.md)\n")
                self.assertTrue(any(e["kind"] == "retired_navigation_restored" and e["path"] == name
                                    for e in inspect_project(self.root)["errors"]))
                (self.root / name).unlink()
                self.refresh_map()
        self.assertTrue(inspect_project(self.root)["passed"])

    def test_active_guides_and_references_reject_broken_links(self):
        for folder in ("guides", "reference"):
            with self.subTest(folder=folder):
                self.write("docs/" + folder + "/sample.md", "# Sample\n[Missing](absent.md)\n")
                result = inspect_project(self.root)
                self.assertTrue(any(e["kind"] == "missing_link_target" for e in result["errors"]))
                self.assertEqual(result["historical_link_warnings"], [])
                (self.root / "docs" / folder / "sample.md").unlink()
                self.refresh_map()

    def test_legacy_document_references_resolve_without_rewriting_records(self):
        self.write("docs/reference/collection_readiness.md", "# Component\n")
        self.write("docs/history/setup.md", "# Previous installation\n")
        self.write("evidence/example.json", '{"evidence":["docs/collection_readiness.md","docs/setup.md","docs/construction_spec.md","docs/collaboration.md"]}\n')
        record = self.root / "evidence/example.json"
        before = record.read_bytes()
        references = json.loads(before)["evidence"]
        self.assertTrue(all(document_reference_path(self.root, ref).is_file() for ref in references))
        self.assertFalse(document_reference_path(self.root, "docs/unrecognized.md").exists())
        self.assertEqual(document_reference_path(self.root, "evidence/example.json"), record)
        self.assertEqual(record.read_bytes(), before)

    def test_current_links_block_but_historical_links_are_reported(self):
        self.write("docs/old_phase.md", "# Historical\n[Old](absent.md)\n")
        self.write("README.md", '# Current\n[Docs](docs/README.md)\n```md\n[Example](not-a-file.md)\n```\n`[Example](also-not-a-file.md)`\n[Web](https://example.invalid/)\n[Fragment](#current)\n')
        result = inspect_project(self.root)
        self.assertTrue(result["passed"], result)
        self.assertEqual(len(result["historical_link_warnings"]), 1)
        self.write("README.md", '# Broken\n[Bad](docs/absent.md)\n')
        self.assertTrue(any(e["kind"] == "missing_link_target" for e in inspect_project(self.root)["errors"]))

    def test_reference_links_and_escaped_paths_checked(self):
        self.write("docs/with space.md", "# Spaces\n")
        self.write("README.md", '# Links\n[Space](<docs/with space.md>)\n[Escaped](docs/with%20space.md)\n[ref]: docs/README.md\n')
        self.assertTrue(inspect_project(self.root)["passed"])
        self.write("README.md", '# Bad reference\n[ref]: docs/missing.md\n')
        self.assertFalse(inspect_project(self.root)["passed"])

    def test_generated_copies_must_match_and_missing_copy_fails(self):
        self.write("docs/mainline_status.json", json.dumps({"stage": 40}))
        result = inspect_project(self.root)
        self.assertEqual(result["generated_status"], "missing_copy")
        self.assertFalse(result["passed"])
        self.write("evidence/mainline/progress.json", json.dumps({"stage": 39}))
        self.assertEqual(inspect_project(self.root)["generated_status"], "different")
        self.write("evidence/mainline/progress.json", json.dumps({"stage": 40}, indent=2))
        self.assertTrue(inspect_project(self.root)["passed"])







    def test_new_document_must_be_registered_exactly_once(self):
        (self.root / "docs/new.md").write_text("# New document\n")
        result = inspect_project(self.root)
        self.assertTrue(any(e["kind"] == "file_map_coverage" for e in result["errors"]))
        self.refresh_map()
        self.assertTrue(inspect_project(self.root)["passed"])
        index = self.root / "docs/README.md"
        index.write_text(index.read_text(encoding="utf-8").replace(MAP_END, "[Again](new.md)\n" + MAP_END), encoding="utf-8")
        self.assertTrue(any(e["kind"] == "duplicate_file_map_entry" for e in inspect_project(self.root)["errors"]))

    def test_missing_map_markers_and_json_omissions_are_rejected(self):
        index = self.root / "docs/README.md"
        original = index.read_text(encoding="utf-8")
        index.write_text(original.replace(MAP_BEGIN, ""), encoding="utf-8")
        self.assertFalse(inspect_project(self.root)["passed"])
        index.write_text(original, encoding="utf-8")
        (self.root / "docs/metadata.json").write_text('{}')
        self.assertTrue(any(e["kind"] == "file_map_coverage" for e in inspect_project(self.root)["errors"]))

    def test_heading_fragments_checked_with_duplicates_and_code_fences(self):
        self.write("docs/headings.md", "# 标题\n## Repeat\n## Repeat\n```md\n## Fake\n```\n")
        self.write("README.md", "# Guide\n[Unicode](docs/headings.md#标题)\n[Second](docs/headings.md#repeat-1)\n")
        self.assertTrue(inspect_project(self.root)["passed"])
        self.write("README.md", "# Bad anchor\n[Code is not a heading](docs/headings.md#fake)\n")
        self.assertTrue(any(e["kind"] == "missing_section_anchor" for e in inspect_project(self.root)["errors"]))


if __name__ == "__main__":
    unittest.main()
