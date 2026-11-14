"""Document grounding and caller-driven environment checks, isolated in temporary roots."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workenv import core,taskpacks,file_adapter
from workenv.office_logic import reference
from workenv.runner import execute
from workenv.sessions import FileSession,SessionProtocol


class DocumentSessions(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name).resolve()
        self.patches=[patch.object(module,"ROOT",self.root) for module in (core,taskpacks,file_adapter)]
        for p in self.patches:p.start()
        core.save(self.root/"tasks/registry.json",{"tasks":[]})
        for variant in ("base","changed_constraint"):
            taskpacks.write_pack(taskpacks.pack("documents",11,variant))

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.directory.cleanup()

    def run_task(self,mode="known_correct",variant="base"):
        return asyncio.run(execute(mode,task_id=f"local-documents-11-{variant}"))

    def test_grounded_documents_and_reordered_rows_pass(self):
        for mode in ("known_correct","alternative","allowed_format"):
            with self.subTest(mode=mode):self.assertIs(self.run_task(mode)["task_passed"],True)

    def test_documents_reject_incorrect_decisions_citations_quotes_report(self):
        for mode in ("empty","claim_only","wrong_object","partial","wrong_amount","wrong_citation","fabricated_quote","wrong_report","missing_artifact","corrupt_artifact","tamper_input"):
            with self.subTest(mode=mode):self.assertIs(self.run_task(mode)["task_passed"],False)

    def test_changed_cutoff_rejects_old_policy_decisions(self):
        self.assertIs(self.run_task(variant="changed_constraint")["task_passed"],True)
        self.assertIs(self.run_task("old_answer","changed_constraint")["task_passed"],False)

    def test_reference_does_not_select_draft_or_future_sources(self):
        result=self.run_task()
        run=core.run_directory(result["run_id"])
        data=(run/"workspace/output/decisions.csv").read_text(encoding="utf-8")
        self.assertNotIn("DRAFT",data)
        self.assertNotIn("FUTURE",data)
        self.assertIn("OPS-CURRENT",data)
        self.assertIn("GLOBAL-CURRENT",data)

    def test_session_starts_without_answers_and_does_not_trust_finish(self):
        with patch("workenv.file_adapter.reference",side_effect=AssertionError("Server must not generate answers")):
            session=FileSession("local-documents-11-base")
        self.assertEqual(list((session.run/"workspace/output").iterdir()),[])
        self.assertFalse(any(e.get("type")=="tool_call" for e in session.trace))
        result=session.finish()
        self.assertIs(result["task_passed"],False)
        meta=core.read_json(session.run/"run.json")
        self.assertEqual(meta["execution_kind"],"external_tool_session")
        self.assertIsNone(meta["model_id"])
        self.assertIsNone(meta["real_model_calls"])
        self.assertEqual(meta["server_model_calls"],0)
        self.assertFalse(meta["training_eligible"])
        self.assertFalse((session.run/".operation.lock").exists())

    def test_session_verifier_corruption_remains_unknown_and_preserves_artifacts(self):
        session=FileSession("local-documents-11-base")
        session.call("write_file",{"path":"output/partial.md","content":"partial"})
        (session.run/"private/inputs/policy.json").write_text("{}",encoding="utf-8")
        result=session.finish()
        self.assertEqual(result["evaluation_status"],"error")
        self.assertIsNone(result["task_passed"])
        self.assertEqual(core.read_json(session.run/"run.json")["artifact_refs"],["workspace/output/partial.md"])
        self.assertFalse((session.run/".operation.lock").exists())

    def test_session_full_round_trip_and_recovery_only_uses_public_tool_inputs(self):
        session=FileSession("local-documents-11-base")
        with self.assertRaises(ValueError):session.call("read_file",{"path":"../private/manifest.json"})
        files=session.call("list_files",{})
        inputs={p.removeprefix("inputs/"):session.call("read_file",{"path":p}) for p in files if p.startswith("inputs/")}
        outputs=reference("documents",inputs)
        for name,text in outputs.items():session.call("write_file",{"path":"output/"+name,"content":text})
        result=session.finish()
        self.assertIs(result["task_passed"],True)
        with self.assertRaises(ValueError):session.call("list_files",{})
        self.assertTrue(any("error" in event for event in session.trace))
        self.assertTrue(all(event["is_model_output"] is None for event in session.trace))

    def test_disconnect_preserves_partial_artifact_without_grade(self):
        session=FileSession("local-documents-11-base")
        session.call("write_file",{"path":"output/partial.md","content":"unfinished"})
        session.close()
        meta=core.read_json(session.run/"run.json")
        self.assertEqual(meta["termination_reason"],"client_disconnected")
        self.assertIsNone(meta["task_passed"])
        self.assertEqual(meta["evaluation_status"],"not_completed")
        self.assertTrue((session.run/"final_state.json").is_file())
        self.assertEqual(meta["artifact_refs"],["workspace/output/partial.md"])

    def test_budget_exhaustion_closes_and_independently_grades(self):
        session=FileSession("local-documents-11-base",max_tool_calls=1)
        session.call("list_files",{})
        with self.assertRaises(ValueError):session.call("list_files",{})
        self.assertTrue(session.finished)
        self.assertEqual(session.status()["termination_reason"],"step_budget_exhausted")
        self.assertIs(session.status()["task_passed"],False)

    def test_protocol_rejects_duplicate_ids_and_recovers_from_bad_arguments(self):
        protocol=SessionProtocol()
        try:
            self.assertTrue(protocol.handle({"id":"1","op":"open","task_id":"local-documents-11-base"})["ok"])
            self.assertFalse(protocol.handle({"id":"1","op":"finish"})["ok"])
            self.assertFalse(protocol.session.finished)
            self.assertFalse(protocol.handle({"id":"2","op":"tool","name":"read_file","arguments":{"path":None}})["ok"])
            self.assertTrue(protocol.handle({"id":"3","op":"tool","name":"list_files","arguments":{}})["ok"])
            self.assertFalse(protocol.handle({"id":"4","op":"open","task_id":"local-documents-11-base"})["ok"])
        finally:protocol.close()

    def test_nested_resources_remain_immutable_and_citations_bounds_rejected(self):
        result=self.run_task()
        run=core.run_directory(result["run_id"])
        path=run/"workspace/output/evidence.json"
        data=core.read_json(path)
        first=next(iter(data.values()))
        first["line_start"]=0
        core.save(path,data)
        adapter=file_adapter.FileTaskAdapter(core.load_task("local-documents-11-base"))
        self.assertIs(adapter.evaluate(run,"known_correct")["task_passed"],False)
        private=next((run/"private/inputs/policies").glob("*.md"))
        private.write_text("tampered",encoding="utf-8")
        with self.assertRaises(ValueError):adapter.evaluate(run,"known_correct")


if __name__=="__main__":unittest.main()
