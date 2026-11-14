"""Explicit file/check fixtures only; no actual task or service acceptance."""
from copy import deepcopy
import contextlib
import importlib.util
import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integrations import archive_inputs as ai, evidence_archive as ea
from integrations.mainline_audit import collect_inputs, read_json, track_input, inputs_current, inventory_snapshot
from workenv.core import save
from workenv.source_inventory import SOURCE_DIRS, MAINLINE_DIRS


class EvidenceSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix="selection-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        for name in (*SOURCE_DIRS,*MAINLINE_DIRS): (self.root/name).mkdir(exist_ok=True)
        for name in ("README.md",".gitignore"): (self.root/name).write_text("unit fixture\n")
        self.source="integrations/unit.py"
        (self.root/self.source).write_text("# explicit source fixture\n")
        self.name="runs/unit/current.json"
        (self.root/self.name).parent.mkdir(parents=True)
        (self.root/self.name).write_text('{"unit":true}')
        self.hashes={n:self.sha(n) for n in (self.source,self.name)}
        contracts={"passed":True,"source_sha256":{self.source:self.hashes[self.source]}}
        save(self.root/"evidence/mainline/contracts.json",contracts)
        raw={"schema_version":"mainline-consistency-audit-v1","passed":True,
             "checks":[{"name":"unit","passed":True}],"input_sha256":self.hashes}
        save(self.root/ea.AUDIT,raw)
        self.old=ea.create(self.root)["archive_ref"]
        comp={"schema_version":"archive-input-equivalence-v1","passed":True,"archive_ref":self.old}
        ref,sha=ai.capture(self.root,comp,"comparisons")
        save(self.root/ai.POLICY,{"schema_version":ai.ACCOUNTING,"workspace_owner":str(self.root),
             "archive_ref":self.old,"comparison_ref":ref,"comparison_sha256":sha})
        self.summary={"tasks_with_current_execution_evidence":["unit"],"training_admissions":0}
        self.draft=self.collect(draft=True)
        save(self.root/ai.DRAFT,self.draft)

    def sha(self,name): return hashlib.sha256((self.root/name).read_bytes()).hexdigest()

    def collect(self,*,draft=False,ref=None):
        with collect_inputs(self.root,selection_draft=draft,archive_ref=ref) as state:
            read_json(self.root/self.name,self.root)
            track_input(self.root/self.source,self.root)
            read_json(self.root/"evidence/mainline/contracts.json",self.root)
            policy=ai.read_policy(self.root)
            read_json(self.root/ai.POLICY,self.root)
            read_json(self.root/policy["comparison_ref"],self.root)
        files=state["files"]
        fields=ai.draft_fields(self.root,files) if draft else ai.fields(self.root,files,state["archive"],state["archived"])
        checks=[{"name":"unit","passed":True},{"name":ai.CHECK,"passed":True},
                {"name":"input_inventory_stable","passed":True,"detail":True}]
        return {"schema_version":ai.DRAFT_SCHEMA if draft else "mainline-consistency-audit-v1",
                "passed":not draft,**({"checks_passed":True} if draft else {}),"checks":checks,
                "real_model_calls":0,"training_updates":0,"input_sha256":files,"expected_absent":[],
                "input_inventory":inventory_snapshot(self.root),"source_sha256":{self.source:self.hashes[self.source]},
                "contracts_sha256":self.sha("evidence/mainline/contracts.json"),**fields}

    def candidate(self):
        self.created=ea.create(self.root,draft=True)
        self.ref=self.created["archive_ref"]
        self.after=self.collect(ref=self.ref)
        save(self.root/ea.AUDIT,self.after)

    def compare(self):
        with patch("training.runtime_capabilities.project_capabilities",return_value={"summary":self.summary}):
            return ai.compare_draft(self.root,self.draft,self.after,self.summary)

    def test_draft_is_fresh_but_never_an_ordinary_passing_audit(self):
        self.assertTrue(ai.draft_current(self.root,self.draft))
        self.assertFalse(inputs_current(self.root,self.draft))
        changed={**self.draft,"schema_version":"mainline-consistency-audit-v1","passed":True}
        self.assertFalse(inputs_current(self.root,changed))
        with self.assertRaises(ValueError): ai.draft_current(self.root,changed)

    def test_capability_projection_rejects_draft_even_with_passed_flag_forged(self):
        from training.runtime_capabilities import _audit
        for passed in (False,True):
            save(self.root/ea.AUDIT,{**self.draft,"passed":passed})
            checks,status=_audit(self.root,{})
            self.assertEqual(checks,{})
            self.assertFalse(status["current"])

    def test_default_collector_and_draft_archive_mode_cannot_be_mixed(self):
        for value in (1,None,"yes"):
            with self.assertRaises(ValueError):
                with collect_inputs(self.root,selection_draft=value):pass
        with self.assertRaises(ValueError):
            with collect_inputs(self.root,selection_draft=True,archive_ref=self.old):pass

    def test_v3_build_preserves_selected_policy_old_zip_and_ordinary_report(self):
        before=[(self.root/n).read_bytes() for n in (ai.POLICY,self.old,ea.AUDIT)]
        result=ea.create(self.root,draft=True)
        self.assertEqual(result["schema_version"],ea.DRAFT_ARCHIVE_SCHEMA)
        self.assertEqual(result["source_audit"]["ref"],ai.DRAFT)
        self.assertEqual(before,[(self.root/n).read_bytes() for n in (ai.POLICY,self.old,ea.AUDIT)])
        self.assertTrue(ea.verify(self.root,result["archive_ref"])["originals_rechecked"])

    def test_invalid_mutually_exclusive_builder_modes_are_rejected(self):
        with self.assertRaises(ValueError):ea.create(self.root,rebase=True,draft=True)
        with self.assertRaises(ValueError):ea.create(self.root,draft=1)

    def test_unknown_failed_duplicate_or_reward_claim_checks_reject_candidate(self):
        for key,value in (("checks_passed",False),("passed",True),("real_model_calls",1),("training_updates",True),
                          ("checks",[{"name":"unit","passed":False}]),("checks",self.draft["checks"]*2)):
            bad={**self.draft,key:value}
            save(self.root/ai.DRAFT,bad)
            with self.assertRaises(ValueError):ea.create(self.root,draft=True)

    def test_changed_or_missing_input_and_contract_source_are_rejected(self):
        for name in (self.name,self.source,"evidence/mainline/contracts.json"):
            path=self.root/name
            before=path.read_bytes()
            path.write_bytes(b"{}")
            with self.assertRaises(ValueError):ai.draft_current(self.root,self.draft)
            path.unlink()
            with self.assertRaises((ValueError,OSError)):ai.draft_current(self.root,self.draft)
            path.write_bytes(before)

    def test_new_program_or_receipt_inventory_invalidates_draft(self):
        new=self.root/"integrations/new.py"
        new.write_text("# unit added\n")
        with self.assertRaisesRegex(ValueError,"inventory changed"):ai.draft_current(self.root,self.draft)
        new.unlink()
        new=self.root/"evidence/mainline/extra/result.json"
        new.parent.mkdir()
        new.write_text("{}")
        with self.assertRaisesRegex(ValueError,"inventory changed"):ai.draft_current(self.root,self.draft)

    def test_wrong_partition_counters_owner_or_hash_are_rejected(self):
        for key,value in (("workspace_owner",str(self.root.parent)),("authority_files",True),
                          ("logical_files",0),("members",[]),("logical_byte_limit",2**40)):
            bad=deepcopy(self.draft)
            bad["selection_draft"][key]=value
            with self.assertRaises(ValueError):ai.draft_current(self.root,bad)
        bad={**self.draft,"logical_input_manifest_sha256":"0"*64}
        with self.assertRaises(ValueError):ai.draft_current(self.root,bad)

    def test_required_absences_cannot_be_replaced_by_old_archived_state(self):
        bad={**self.draft,"expected_absent":[self.name]}
        with self.assertRaises(ValueError):ai.draft_current(self.root,bad)

    def test_collection_detects_changed_bytes_and_actual_content_limit(self):
        with collect_inputs(self.root,selection_draft=True):
            read_json(self.root/self.name,self.root)
            (self.root/self.name).write_text('{"unit":false}')
            with self.assertRaisesRegex(ValueError,"changed during collection"):read_json(self.root/self.name,self.root)
        with patch.object(ea,"MAX_CONTENT",1):
            with collect_inputs(self.root,selection_draft=True):
                with self.assertRaisesRegex(ValueError,"capacity"):read_json(self.root/self.name,self.root)

    def test_8191_logical_boundary_keeps_4096_group_limits_and_reserved_authority(self):
        members=self.root/"runs/boundary"
        direct=self.root/".runtime/unit-direct"
        members.mkdir()
        direct.mkdir(parents=True)
        with collect_inputs(self.root,selection_draft=True) as state:
            for directory,count in ((members,4096),(direct,4095)):
                for index in range(count):
                    path=directory/(str(index)+".json")
                    path.write_bytes(b"{}")
                    track_input(path,self.root)
            fields=ai.draft_fields(self.root,state["files"])
            self.assertEqual(fields["selection_draft"]["authority_files"],4096)
            self.assertEqual(fields["selection_draft"]["logical_files"],8191)
            extra=direct/"overflow.json"
            extra.write_bytes(b"{}")
            with self.assertRaisesRegex(ValueError,"capacity"):track_input(extra,self.root)
        with collect_inputs(self.root):
            for path in list(members.iterdir())[:4096]:track_input(path,self.root)
            with self.assertRaisesRegex(ValueError,"inventory exceeds"):track_input(direct/"0.json",self.root)

    def test_4097_members_fail_even_when_direct_authorities_fit(self):
        files={"runs/unit/"+str(i)+".json":"a"*64 for i in range(4097)}
        # Reuse actual fixture bytes through a narrow stat-only boundary fixture.
        with patch.object(Path,"stat",return_value=(self.root/self.name).stat()),patch.object(Path,"is_file",return_value=True):
            with self.assertRaisesRegex(ValueError,"capacity"):ai.draft_fields(self.root,files)

    def test_drafts_cannot_import_an_old_zip_as_a_current_input(self):
        with self.assertRaisesRegex(ValueError,"previous archive ZIP"):
            ai.draft_fields(self.root,{self.old:self.sha(self.old)})

    def test_v3_rechecks_retained_originals_after_live_draft_changes(self):
        self.candidate()
        save(self.root/ai.DRAFT,{})
        self.assertTrue(ea.verify(self.root,self.ref)["passed"])
        (self.root/self.name).write_text("{}")
        with self.assertRaises(ValueError):ea.verify(self.root,self.ref)

    def test_equivalence_requires_current_normal_report_and_qualification(self):
        self.candidate()
        result=self.compare()
        self.assertEqual(result["schema_version"],"draft-authority-equivalence-v1")
        self.assertFalse(result["draft_is_task_acceptance"])
        self.assertEqual(result["matched_logical_inputs"],len(self.draft["input_sha256"]))
        with patch("training.runtime_capabilities.project_capabilities",return_value={"summary":{}}):
            with self.assertRaisesRegex(ValueError,"qualification"):
                ai.compare_draft(self.root,self.draft,self.after,self.summary)

    def test_equivalence_rejects_changed_business_checks_context_and_read_set(self):
        self.candidate()
        for key,value in (("checks",[{"name":"other","passed":True}]),("contracts_sha256","a"*64)):
            after={**self.after,key:value}
            save(self.root/ea.AUDIT,after)
            with patch("training.runtime_capabilities.project_capabilities",return_value={"summary":self.summary}):
                with self.assertRaises(ValueError):ai.compare_draft(self.root,self.draft,after,self.summary)
        bad=deepcopy(self.draft)
        bad["input_sha256"].pop(self.source)
        bad.update(ai.draft_fields(self.root,bad["input_sha256"]))
        save(self.root/ea.AUDIT,self.after)
        with patch("training.runtime_capabilities.project_capabilities",return_value={"summary":self.summary}):
            with self.assertRaisesRegex(ValueError,"logical evidence"):
                ai.compare_draft(self.root,bad,self.after,self.summary)

    def test_atomic_activation_requires_policy_sha_and_retains_provenance(self):
        self.candidate()
        comparison=self.compare()
        old=ai.policy_sha256(self.root)
        with self.assertRaises(ValueError):ai.activate(self.root,self.after,comparison,expected_policy_sha256="0"*64)
        self.assertEqual(ai.policy_sha256(self.root),old)
        with patch("training.runtime_capabilities.project_capabilities",return_value={"summary":self.summary}):
            policy=ai.activate(self.root,self.after,comparison,expected_policy_sha256=old)
        self.assertEqual(ai.read_policy(self.root),policy)
        self.assertEqual(policy["archive_ref"],self.ref)
        self.assertFalse(inputs_current(self.root,self.after))
        self.assertTrue(inputs_current(self.root,self.collect(ref=self.ref)))

    def test_atomic_failure_or_forged_comparison_preserves_old_policy(self):
        self.candidate()
        comparison=self.compare()
        old=(self.root/ai.POLICY).read_bytes()
        with patch("training.runtime_capabilities.project_capabilities",return_value={"summary":self.summary}):
            with self.assertRaises(ValueError):
                ai.activate(self.root,self.after,{**comparison,"matched_logical_inputs":0},expected_policy_sha256=ai.policy_sha256(self.root))
            with patch("workenv.core.replace_atomic",side_effect=OSError("unit interrupted")):
                with self.assertRaises(OSError):
                    ai.activate(self.root,self.after,comparison,expected_policy_sha256=ai.policy_sha256(self.root))
        self.assertEqual((self.root/ai.POLICY).read_bytes(),old)

    def test_cli_rejects_missing_ordinary_report_before_candidate_run(self):
        script=Path(__file__).resolve().parents[1]/"scripts/audit_mainline.py"
        spec=importlib.util.spec_from_file_location("selection_cli_fixture",script)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        (self.root/ea.AUDIT).unlink()
        old=ai.policy_sha256(self.root)
        with patch.object(module,"ROOT",self.root),patch("sys.argv",[str(script),"--compare-selection-draft",self.old]),\
             patch.object(module,"run_audit") as run,contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_code:module.main()
            self.assertEqual(exit_code.exception.code,2)
            run.assert_not_called()
        self.assertEqual(ai.policy_sha256(self.root),old)
        self.assertFalse((self.root/ea.AUDIT).exists())


if __name__=="__main__":unittest.main()
