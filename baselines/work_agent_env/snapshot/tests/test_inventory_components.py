"""Explicit file/RPC fixtures for inventory composition, without PG or MCP."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from integrations import inventory_components as component
from integrations import inventory_data as data
from integrations.episode_cluster import EpisodeProfile, EpisodeCluster
from tests.test_inventory_data import snapshot_fixture, response
from workenv.core import ResourceError, save


def event_args():
    return {"summary": "Diagnostic restock", "description": "A literal description", "start": {"dateTime": "2026-03-10T13:00:00Z", "timeZone": "America/New_York"},
            "end": {"dateTime": "2026-03-10T14:00:00Z", "timeZone": "America/New_York"}}


def after_create():
    state = snapshot_fixture(); args = event_args()
    state["events"] = [{"id": str(uuid4()), "summary": args["summary"], "description": args["description"],
        "start_datetime": args["start"]["dateTime"], "end_datetime": args["end"]["dateTime"],
        "start_timezone": args["start"]["timeZone"], "end_timezone": args["end"]["timeZone"]}]
    return state


class InventoryCompositionBoundaryTests(unittest.TestCase):
    def test_component_audit_refuses_reward_and_hard_interruption_claims(self):
        from integrations.inventory_components_audit import audit
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); path = root / 'result.json'
            for changed in ({'reward': 1}, {'hard_interruption_verified': True}):
                save(path, {'schema_version': 'inventory-original-components-v1', 'passed': True, **changed})
                with self.assertRaises(ValueError): audit(root, path)

    def test_calendar_error_observation_cannot_be_a_committed_creation(self):
        with self.assertRaises(ResourceError):
            component.verify_effect('google_calendar', 'create_event', event_args(), {'content': [{'type': 'text', 'text': 'Error creating event'}]}, snapshot_fixture(), after_create())

    def test_only_fixed_methods_and_product_paging_are_allowed(self):
        component.validate_call("woocommerce", "woo_products_list", {"page": 1, "perPage": 10})
        for server, name, args in (("woocommerce", "woo_products_delete", {}), ("google_calendar", "delete_event", {}),
            ("filesystem", "read_file", {}), ("woocommerce", "woo_products_list", {"page": True, "perPage": 10}),
            ("woocommerce", "woo_products_list", {"page": 1, "perPage": 100})):
            with self.subTest(server=server, name=name), self.assertRaises(ValueError): component.validate_call(server, name, args)

    def test_calendar_scope_excludes_attachments_attendees_and_missing_offsets(self):
        component.validate_call("google_calendar", "create_event", event_args())
        for changed in ({**event_args(), "attendees": ["outside@example.com"]},
            {**event_args(), "start": {"dateTime": "2026-03-10T13:00:00", "timeZone": "UTC"}},
            {**event_args(), "summary": "bad\nline"}):
            with self.assertRaises(ValueError): component.validate_call("google_calendar", "create_event", changed)

    def test_business_wrong_but_structural_event_is_measurable(self):
        changed = event_args(); changed["start"]["dateTime"] = "2026-03-10T13:30:00Z"
        component.validate_call("google_calendar", "create_event", changed)

    def test_list_budget_event_uuid_and_email_transport_are_restricted(self):
        for name, args in (("get_event", {"eventId": "foreign"}), ("list_events", {"timeMin": "2026-03-10T00:00:00Z", "timeMax": "2026-03-11T00:00:00Z", "maxResults": 1000})):
            with self.assertRaises(ValueError): component.validate_call("google_calendar", name, args)
        with self.assertRaises(ValueError): component.validate_call("email", "send_email", {"to": "a@example.com", "subject": "s", "body": "b", "attachments": ["secret"]})

    def test_calendar_create_requires_exact_single_effect(self):
        before = snapshot_fixture(); after = after_create()
        component.verify_effect("google_calendar", "create_event", event_args(), response([]), before, after)
        for wrong in (before, {**after, "events": after["events"] * 2}):
            with self.assertRaises(ResourceError): component.verify_effect("google_calendar", "create_event", event_args(), response([]), before, wrong)

    def test_calendar_success_cannot_hide_wrong_text_time_or_mail_effect(self):
        for field in ("summary", "start_datetime", "start_timezone", "email"):
            wrong = after_create()
            if field == "email": wrong["email"]["messages"] = [{"id": 99}]
            else: wrong["events"][0][field] = "2026-03-10T13:30:00Z" if field == "start_datetime" else "wrong"
            with self.assertRaises((ResourceError, ValueError)): component.verify_effect("google_calendar", "create_event", event_args(), response([]), snapshot_fixture(), wrong)

    def test_read_operation_and_provider_error_never_commit_side_effects(self):
        with self.assertRaises(ResourceError): component.verify_effect("google_calendar", "list_events", {}, response([]), snapshot_fixture(), after_create())
        with self.assertRaises(ResourceError): component.verify_effect("google_calendar", "create_event", event_args(), {"isError": True}, snapshot_fixture(), after_create())

    def test_compact_snapshot_rejects_changed_source_bytes_and_foreign_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); initial = root / "initial.json"; path = root / "snapshot.json"
            save(initial, snapshot_fixture()); component.save_snapshot(root, path, after_create(), initial)
            self.assertEqual(component.load_snapshot(root, path, initial)["products"], snapshot_fixture()["products"])
            original = initial.read_bytes(); initial.write_bytes(original + b"\n")
            with self.assertRaises(ResourceError): component.load_snapshot(root, path, initial)
            initial.write_bytes(original); record = json.loads(path.read_text()); record["source_ref"] = "foreign.json"; save(path, record)
            with self.assertRaises(ResourceError): component.load_snapshot(root, path, initial)

    def test_compact_writer_does_not_replace_the_readonly_truth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); initial = root / "initial.json"; save(initial, snapshot_fixture())
            changed = snapshot_fixture(); changed["products"] = []
            with self.assertRaises(ResourceError): component.save_snapshot(root, root / "snapshot.json", changed, initial)

    def test_direct_database_reset_cannot_bypass_prepared_calendar_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); profile = EpisodeProfile(root, str(uuid4()), 0)
            profile.base.mkdir(parents=True); save(profile.base / "cluster_owner.json", profile.record())
            cluster = EpisodeCluster(profile); cluster.started = True
            journal = profile.base / "inventory-components-run/requests.json"
            save(journal, {"schema_version": component.JOURNAL_SCHEMA, "task_id": data.TASK_ID, "profile": profile.record(), "entries": {str(uuid4()): {"state": "prepared"}}})
            original = journal.read_bytes()
            with patch.object(data.subprocess, "run") as run, self.assertRaisesRegex(ResourceError, "reset refused"):
                data.initialize(root, cluster, resetting=True)
            run.assert_not_called(); self.assertEqual(journal.read_bytes(), original)


class InventoryCompositionRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(); profile = EpisodeProfile(self.root, str(uuid4()), 0)
        self.cluster = Mock(profile=profile, started=True)
        path = self.root / "evidence/mainline/calendar_build.json"; save(path, {"build_fingerprint": "fixture"})
        with patch.object(component, "verify_build"): self.controller = component.InventoryComponents(self.root, self.cluster, {})
        c = self.controller; c.run.mkdir(parents=True); c.initial = snapshot_fixture(); save(c.run / "initial_snapshot.json", c.initial)
        save(c.run / "requests.json", c.requests)
        self.guard_patch = patch.object(c, "guard"); self.guard_patch.start(); self.addCleanup(self.guard_patch.stop)
        self.client = Mock(call_tool=AsyncMock(return_value=Mock(model_dump=lambda **kw: response([]))))
        c.clients = {"google_calendar": self.client}

    async def test_intent_precedes_rpc_and_cached_uuid_does_not_duplicate(self):
        c = self.controller; rid = str(uuid4())
        async def invoke(*_):
            self.assertEqual(json.loads((c.run / "requests.json").read_text())["entries"][rid]["state"], "prepared")
            return Mock(model_dump=lambda **kw: response([]))
        self.client.call_tool.side_effect = invoke
        with patch.object(data, "snapshot", side_effect=[snapshot_fixture(), after_create()]):
            reply = await c.call("google_calendar", "create_event", event_args(), rid)
            self.assertEqual(reply, await c.call("google_calendar", "create_event", event_args(), rid))
        self.client.call_tool.assert_awaited_once()

    async def test_uuid_conflict_is_rejected_without_a_second_rpc(self):
        c = self.controller; rid = str(uuid4())
        with patch.object(data, "snapshot", side_effect=[snapshot_fixture(), after_create()]): await c.call("google_calendar", "create_event", event_args(), rid)
        with self.assertRaises(ValueError): await c.call("google_calendar", "create_event", {**event_args(), "summary": "different"}, rid)
        self.client.call_tool.assert_awaited_once()

    async def test_foreign_event_rejected_before_intent_or_rpc(self):
        with patch.object(data, "snapshot", return_value=snapshot_fixture()), self.assertRaises(ResourceError):
            await self.controller.call("google_calendar", "get_event", {"eventId": str(uuid4())}, str(uuid4()))
        self.client.call_tool.assert_not_awaited(); self.assertFalse(self.controller.requests["entries"])

    async def test_rpc_failure_keeps_unknown_with_observation_and_never_replays(self):
        self.client.call_tool.side_effect = RuntimeError("lost transport")
        with patch.object(data, "snapshot", return_value=snapshot_fixture()), self.assertRaises(ResourceError):
            await self.controller.call("google_calendar", "create_event", event_args(), str(uuid4()))
        self.assertTrue(self.controller.unknown)
        entry = next(iter(self.controller.requests["entries"].values()))
        self.assertEqual(entry["state"], "unknown"); self.assertIn("after_ref", entry)

    async def test_invalid_operation_does_not_create_request(self):
        with self.assertRaises(ValueError): await self.controller.call("filesystem", "read_file", {}, str(uuid4()))
        self.assertFalse(self.controller.requests["entries"]); self.client.call_tool.assert_not_awaited()


if __name__ == "__main__": unittest.main()
