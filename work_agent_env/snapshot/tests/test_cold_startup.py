"""Cold restart orchestration fixtures never stop or start real services."""
from contextlib import ExitStack, redirect_stdout
import hashlib
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from integrations.startup_audit import COLD_STAGES, LINUX_COLD_STAGES, LEDGERS, audit_linux_processes, task_command_receipt

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
    startup = importlib.import_module("scripts.verify_mainline_startup")
    ensure = importlib.import_module("scripts.ensure_mainline_runtime")


class ColdStartupTests(unittest.TestCase):
    def test_readonly_guest_probe_timeout_is_not_a_fatal_startup_result(self):
        with patch.object(ensure.subprocess, "run", side_effect=subprocess.TimeoutExpired(["ssh"], 15)) as command:
            self.assertIsNone(ensure.guest_probe())
        self.assertEqual(command.call_count, 1)

    def test_transient_probe_waits_for_readiness_without_replaying_launch(self):
        pending = ({"ready": False, "active_session": None}, None, None)
        ready = ({"ready": True}, {}, {})
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", ["ensure_mainline_runtime.py"]))
            stack.enter_context(patch.object(ensure, "observe", side_effect=[pending, pending, ready]))
            stack.enter_context(patch.object(ensure, "ensure_vm"))
            stack.enter_context(patch.object(ensure, "guest_probe", return_value=None))
            stack.enter_context(patch.object(ensure.time, "sleep"))
            launch = stack.enter_context(patch.object(ensure, "deploy"))
            bridge = stack.enter_context(patch.object(ensure, "start_bridge"))
            stack.enter_context(redirect_stdout(io.StringIO()))
            self.assertEqual(ensure.main(), 0)
        launch.assert_not_called()
        bridge.assert_not_called()

    def test_repeated_unknown_probe_still_hits_overall_startup_deadline(self):
        pending = ({"ready": False, "active_session": None}, None, None)
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", ["ensure_mainline_runtime.py", "--timeout", "10"]))
            stack.enter_context(patch.object(ensure, "observe", return_value=pending))
            stack.enter_context(patch.object(ensure, "ensure_vm"))
            stack.enter_context(patch.object(ensure, "guest_probe", return_value=None))
            stack.enter_context(patch.object(ensure.time, "monotonic", side_effect=[0, 1, 11]))
            stack.enter_context(patch.object(ensure.time, "sleep"))
            launch = stack.enter_context(patch.object(ensure, "deploy"))
            with self.assertRaises(TimeoutError):
                ensure.main()
        launch.assert_not_called()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vm = self.root / ".runtime/nemo-vm"
        self.vm.mkdir(parents=True)
        (self.root / "evidence/mainline").mkdir(parents=True)
        self.ledger_bytes = b'{"sessions": []}\n'
        for name in LEDGERS: (self.vm / name).write_bytes(self.ledger_bytes)
        self.idle = {"runtime_ready": True, "active_session": None,
                     "active_synthetic_sessions": [], "active_isolated_sessions": []}
        self.operations = []
        self.vm_record(1)

    def vm_record(self, number):
        record = {"pid": number, "command": ["qemu.exe", "-name", "workagent-nemo-v2"], "started_unix": number}
        (self.vm / "process.json").write_text(json.dumps(record), encoding="utf-8")

    def attest(self):
        self.operations.append("attest_vm")
        raw = (self.vm / "process.json").read_bytes()
        record = json.loads(raw)
        return {"record": record, "record_sha256": hashlib.sha256(raw).hexdigest(),
                "identity": {"pid": record["pid"], "status": "alive", "creation_id": str(record["pid"])}}

    def command(self, directory, name, command, timeout):
        self.operations.append(name)
        if name == "restart_runtime": self.vm_record(2)
        if name == "warm_idempotence":
            (self.root / "evidence/mainline/startup_idempotence.json").write_text(json.dumps({"passed": True, "before": {}, "after": {}}))
        return {"stdout_ref": "unused-fixture-output"}

    def task(self, kind, command):
        self.operations.append("audit_" + kind)
        return {"receipt_ref": "fixture-" + kind, "sha256": "0" * 64}

    def invoke(self, *, observed=None, command=None, waiter=None, native=False):
        def wait(identity, timeout):
            self.operations.append("wait_vm_exit")
            return {"pid": identity["pid"], "status": "not_found"}
        with ExitStack() as stack:
            stack.enter_context(patch.object(startup, "ROOT", self.root))
            stack.enter_context(patch.object(startup, "VM", self.vm))
            stack.enter_context(patch.object(startup, "cold_state", return_value=self.idle if observed is None else observed))
            stack.enter_context(patch.object(startup, "attest_linux" if native else "attest_vm", side_effect=self.attest))
            stack.enter_context(patch.object(startup, "cold_command", side_effect=command or self.command))
            stack.enter_context(patch.object(startup, "verify_cold_task", side_effect=self.task))
            stack.enter_context(patch.object(startup, "wait_vm_exit", side_effect=waiter or wait))
            stack.enter_context(patch("integrations.startup_audit.cold_source_identity", return_value={"scripts/fixture.py": "0" * 64}))
            stack.enter_context(redirect_stdout(io.StringIO()))
            code = startup.verify_cold(startup_timeout=10, shutdown_timeout=1, native=native)
        report = json.loads((self.root / "evidence/mainline/cold_startup.json").read_text(encoding="utf-8"))
        self.assertFalse((self.vm / ".cold-startup.lock").exists())
        return code, report

    def test_each_active_or_unknown_resource_group_refuses_before_any_stop(self):
        cases = ({"active_session": "resource"}, {"active_synthetic_sessions": ["resource"]},
                 {"active_isolated_sessions": ["resource"]}, {"runtime_ready": False},
                 {"active_synthetic_sessions": None}, {"active_isolated_sessions": None})
        for change in cases:
            with self.subTest(change=change):
                code, report = self.invoke(observed={**self.idle, **change})
                self.assertEqual(code, 1)
                self.assertEqual(self.operations, [])
                self.assertFalse(report["passed"])
                self.assertEqual(report["stages"], [{**report["stages"][0], "name": COLD_STAGES[0], "state": "failed"}])
                self.assertTrue(all((self.vm / name).read_bytes() == self.ledger_bytes for name in LEDGERS))

    def test_success_orders_stops_before_restart_then_tasks_and_warm_check(self):
        code, report = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(self.operations, ["attest_vm", "stop_services", "stop_vm", "wait_vm_exit", "restart_runtime",
                                          "attest_vm", "tax_task", "audit_tax", "meal_task", "audit_meal", "warm_idempotence"])
        self.assertEqual([s["name"] for s in report["stages"]], list(COLD_STAGES))
        self.assertTrue(all(s["state"] == "completed" for s in report["stages"]))
        retained = self.root / report["report_ref"]
        self.assertEqual(json.loads(retained.read_text()), report)
        journal = [json.loads(line) for line in (retained.parent / "stages.jsonl").read_text().splitlines()]
        self.assertEqual(journal[1::2], report["stages"])
        self.assertTrue(all((self.vm / name).read_bytes() == self.ledger_bytes for name in LEDGERS))

    def test_stop_failure_preserves_failed_attempt_without_further_mutations(self):
        def fail(directory, name, command, timeout):
            self.operations.append(name)
            raise RuntimeError("fixture stop failure")
        code, report = self.invoke(command=fail)
        self.assertEqual(code, 1)
        self.assertEqual(self.operations, ["attest_vm", "stop_services"])
        self.assertEqual(report["stages"][-1]["state"], "failed")
        self.assertEqual(report["error"], "fixture stop failure")
        self.assertTrue(all((self.vm / name).read_bytes() == self.ledger_bytes for name in LEDGERS))

    def test_shutdown_timeout_does_not_restart_or_force_the_vm(self):
        def wait(identity, timeout):
            self.operations.append("wait_vm_exit")
            raise TimeoutError("fixture owned VM is still alive")
        code, report = self.invoke(waiter=wait)
        self.assertEqual(code, 1)
        self.assertEqual(self.operations, ["attest_vm", "stop_services", "stop_vm", "wait_vm_exit"])
        self.assertEqual(report["error_type"], "TimeoutError")
        self.assertFalse(report["forced_runtime_process_termination"])

    def test_changed_ledger_stops_before_tasks_and_does_not_restore_old_bytes(self):
        def command(directory, name, argv, timeout):
            value = self.command(directory, name, argv, timeout)
            if name == "restart_runtime": (self.vm / LEDGERS[0]).write_bytes(b'{"unexpected": true}\n')
            return value
        code, report = self.invoke(command=command)
        self.assertEqual(code, 1)
        self.assertEqual(report["stages"][-1]["name"], "ledger_bytes_preserved")
        self.assertNotIn("tax_task", self.operations)
        self.assertEqual((self.vm / LEDGERS[0]).read_bytes(), b'{"unexpected": true}\n')
        self.assertEqual((self.root / report["ledgers_before"][LEDGERS[0]]["ref"]).read_bytes(), self.ledger_bytes)

    def test_unknown_or_rebound_pid_is_not_vm_exit_proof(self):
        original = {"pid": 27, "status": "alive", "creation_id": "known"}
        for observed in ({"pid": 27, "status": "unknown", "creation_id": None},
                         {"pid": 27, "status": "alive", "creation_id": "changed"},
                         {"pid": 28, "status": "not_found"}):
            with self.subTest(observed=observed), patch.object(startup, "process_identity", return_value=observed):
                with self.assertRaises(RuntimeError): startup.wait_vm_exit(original, 1)

    def test_timed_out_command_keeps_its_output_and_unknown_exit_status(self):
        directory = self.root / "command-attempt"
        directory.mkdir()
        def timed_out(command, **kwargs):
            kwargs["stdout"].write(b"partial startup observation\n")
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        with patch.object(startup, "ROOT", self.root), patch.object(startup.subprocess, "run", side_effect=timed_out):
            with self.assertRaises(subprocess.TimeoutExpired): startup.cold_command(directory, "start", ["python", "fixture"], 10)
        command = json.loads((directory / "start.command.json").read_text())
        self.assertIsNone(command["exit_code"])
        self.assertEqual(command["error_type"], "TimeoutExpired")
        self.assertEqual((self.root / command["stdout_ref"]).read_bytes(), b"partial startup observation\n")

    def test_default_cli_still_runs_only_warm_idempotence(self):
        with patch.object(sys, "argv", ["verify_mainline_startup.py"]), patch.object(startup, "verify_warm", return_value=0) as warm, patch.object(startup, "verify_cold") as cold:
            self.assertEqual(startup.main(), 0)
        warm.assert_called_once_with()
        cold.assert_not_called()

    def task_receipt_fixture(self, kind="tax", at="2026-10-17T12:00:01+00:00"):
        prefix = "nemo-diagnostic-" if kind == "tax" else "nemo-meal-plan-"
        path = self.root / ("evidence/mainline/" + prefix + "fixture/result.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": at}), encoding="utf-8")
        reference = path.relative_to(self.root).as_posix()
        binding = {"receipt_ref": reference, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        command = {"started_at": "2026-10-17T12:00:00+00:00", "finished_at": "2026-10-17T12:00:02+00:00"}
        output = {"evidence": path.parent.relative_to(self.root).as_posix()} if kind == "tax" else {"receipt": reference}
        return path, binding, command, output

    def test_task_receipts_bind_both_actual_stdout_contracts(self):
        for kind in ("tax", "meal"):
            with self.subTest(kind=kind):
                path, binding, command, output = self.task_receipt_fixture(kind)
                self.assertEqual(task_command_receipt(self.root, kind, binding, command, output), path)

    def test_task_receipt_from_another_command_or_time_is_rejected(self):
        path, binding, command, output = self.task_receipt_fixture()
        with self.assertRaisesRegex(ValueError, "rebound"):
            task_command_receipt(self.root, "tax", binding, command, {"evidence": "evidence/mainline/nemo-diagnostic-other"})
        for at in ("2026-10-17T11:59:59+00:00", "2026-10-17T12:00:03+00:00", "2026-10-17T12:00:01"):
            with self.subTest(at=at):
                _, binding, command, output = self.task_receipt_fixture(at=at)
                with self.assertRaisesRegex(ValueError, "outside"):
                    task_command_receipt(self.root, "tax", binding, command, output)

    def test_task_receipt_byte_change_is_rejected(self):
        path, binding, command, output = self.task_receipt_fixture()
        path.write_text(json.dumps({"at": "2026-10-17T12:00:01+00:00", "replaced": True}))
        with self.assertRaisesRegex(ValueError, "receipt changed"):
            task_command_receipt(self.root, "tax", binding, command, output)


class LinuxColdStartupTests(unittest.TestCase):
    setUp = ColdStartupTests.setUp
    vm_record = ColdStartupTests.vm_record
    invoke = ColdStartupTests.invoke
    task = ColdStartupTests.task

    def attest(self):
        number = len([name for name in self.operations if name == "attest_linux"]) + 1
        self.operations.append("attest_linux")
        result = {}
        for bridge in (False, True):
            pid = number * 100 + int(bridge)
            identity = {"pid": pid, "status": "alive", "creation_id": f"linux-proc:00000000-0000-0000-0000-000000000000:{pid}"}
            record = {"pid": pid, "process_identity": identity, "command": [str(self.root / ".venv/bin/python"), "-m", "integrations.toolathlon_bridge"]} if bridge else {
                "pid": pid, "identity": identity, "workspace": str(self.root), "source": str(self.root / "integrations/nemo_resources.py")}
            raw = json.dumps(record).encode()
            (self.vm / ("bridge-process.json" if bridge else "nemo-process.json")).write_bytes(raw)
            observation = {"record": record, "identity": identity, "record_sha256": hashlib.sha256(raw).hexdigest()}
            if bridge: result["bridge"] = observation
            else: result.update(observation)
        return result

    def command(self, directory, name, argv, timeout):
        self.assertNotIn("nemo_vm.py", " ".join(argv))
        self.operations.append(name)
        if name in ("stop_services", "restart_runtime"):
            self.assertEqual(Path(argv[1]).name, "linux_mainline.py")
            self.assertEqual(argv[2:], ["stop" if name == "stop_services" else "start"])
        if name == "warm_idempotence":
            (self.root / "evidence/mainline/startup_idempotence.json").write_text(json.dumps({"passed": True, "before": {}, "after": {}}))
        return {"stdout_ref": "unused-fixture-output"}

    def test_native_restart_confirms_both_process_exits_without_a_vm_command(self):
        code, report = self.invoke(native=True)
        self.assertEqual(code, 0)
        self.assertEqual([s["name"] for s in report["stages"]], list(LINUX_COLD_STAGES))
        self.assertEqual(self.operations, ["attest_linux", "stop_services", "wait_vm_exit", "wait_vm_exit", "restart_runtime",
                                           "attest_linux", "tax_task", "audit_tax", "meal_task", "audit_meal", "warm_idempotence"])
        self.assertEqual(report["bridge_exit"]["pid"], 101)
        self.assertEqual(report["nemo_exit"]["pid"], 100)
        self.assertTrue(all((self.vm / name).read_bytes() == self.ledger_bytes for name in LEDGERS))

    def test_unknown_idle_state_refuses_native_shutdown(self):
        code, report = self.invoke(native=True, observed={**self.idle, "active_session": "someone-else"})
        self.assertEqual(code, 1)
        self.assertEqual(self.operations, [])
        self.assertFalse(report["passed"])

    def test_native_exit_timeout_preserves_state_without_restart(self):
        def wait(identity, timeout): raise TimeoutError("bridge still alive")
        code, report = self.invoke(native=True, waiter=wait)
        self.assertEqual(code, 1)
        self.assertEqual(self.operations, ["attest_linux", "stop_services"])
        self.assertEqual(report["stages"][-1]["name"], "confirm_bridge_exit")

    def test_native_audit_rejects_foreign_workspace_and_reused_process(self):
        code, report = self.invoke(native=True)
        self.assertEqual(code, 0)
        for key in ("before", "after_restart"):
            runtime = report["runtime_before" if key == "before" else "runtime_after"]
            report[key].update(nemo_pid=runtime["identity"]["pid"], bridge_pid=runtime["bridge"]["identity"]["pid"])
        read = lambda ref: json.loads((self.root / ref).read_text())
        audit_linux_processes(self.root, report, read, report["retained_sha256"])
        report["runtime_after"]["record"]["workspace"] = "another-workspace"
        with self.assertRaisesRegex(ValueError, "ownership"):
            audit_linux_processes(self.root, report, read, report["retained_sha256"])

    def test_cli_selects_native_restart_on_linux(self):
        with patch.object(sys, "argv", ["verify_mainline_startup.py", "--cold-restart"]), patch.object(sys, "platform", "linux"), patch.object(startup, "verify_cold", return_value=0) as cold:
            self.assertEqual(startup.main(), 0)
        cold.assert_called_once_with(startup_timeout=600, shutdown_timeout=90, native=True)


if __name__ == "__main__": unittest.main()
