"""Static public-source inspection never executes scripts or exports DB secrets."""
import json
import unittest

from training.task_lifecycle_design import MEMBERS, python_contract, service_markers


class LifecycleDesignTests(unittest.TestCase):
    def test_ast_extracts_isolation_dependencies_without_execution_or_passwords(self):
        source = b'''import os
import psycopg2
DB_CONFIG={"password":"do-not-export-this-value", "port":5432, "host":os.environ.get("PGHOST")}
def main(cur):
    cur.execute("DELETE FROM gcal.events")
    filename="Weekly_Meal_Plan.xlsx"
raise RuntimeError("must never execute")
'''
        report = python_contract(source)
        self.assertEqual(report["literal_sql_tables"], ["gcal.events"])
        self.assertEqual(report["environment_names"], ["PGHOST"])
        self.assertEqual(report["literal_database_port"], 5432)
        self.assertEqual(report["artifact_basenames"], ["Weekly_Meal_Plan.xlsx"])
        self.assertNotIn("do-not-export", json.dumps(report))

    def test_dynamic_sql_and_port_are_not_claimed_as_resolved(self):
        report = python_contract(b'import os\nDB_CONFIG={"port":int(os.environ.get("PGPORT", "5432"))}\ncur.execute(build_query())')
        self.assertEqual(report["literal_sql_tables"], [])
        self.assertIsNone(report["literal_database_port"])
        self.assertIn("PGPORT", report["environment_names"])

    def test_typescript_markers_only_report_names_and_fetch_presence(self):
        report = service_markers(b"import localRecipes from './all_recipes.json'; const secret = process.env.PG_PASSWORD; const result=await fetch(url);")
        self.assertEqual(report["environment_names"], ["PG_PASSWORD"])
        self.assertTrue(report["fetch_call_marker"])
        self.assertTrue(report["local_json_import_marker"])

    def test_member_scope_excludes_solutions_task_bodies_and_verified(self):
        self.assertEqual(len(MEMBERS), 4)
        for member in MEMBERS.values():
            self.assertNotIn("groundtruth", member)
            self.assertNotIn("docs/task.md", member)
            self.assertNotIn("verified", member)
        with self.assertRaises(ValueError): python_contract(b"x" * (256 * 1024 + 1))
