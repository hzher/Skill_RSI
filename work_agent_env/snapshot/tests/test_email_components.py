"""Original email SQL effects and durable request boundaries, without mail/network."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from adapters.email_pg_native import ToolRegistrar, require_pg_environment, SENDER
from integrations import email_components as email
from integrations.episode_cluster import EpisodeProfile
from workenv.core import ResourceError, save


def response(text): return {"content": [{"type": "text", "text": text}], "isError": False}


def empty(): return {"office": {"readonly_sha256": "fixture", "cells": []}, "email": {k: [] for k in email.TABLES}}


def sent():
    data = empty()
    data["email"]["messages"] = [{"id": 1, "folder_id": 1, "from_addr": SENDER, "to_addr": ["a@example.com"],
        "subject": "Local", "body_text": "Hello", "cc_addr": [], "bcc_addr": [], "body_html": None, "is_read": False}]
    data["email"]["folders"] = [{"id": 1, "name": "Sent", "message_count": 0, "unread_count": 0}]
    data["email"]["sent_log"] = [{"id": 1, "message_id": 1}]
    return data


ARGS = {"to": "a@example.com", "subject": "Local", "body": "Hello"}
OK = response("Email sent successfully to a@example.com")


class EmailEffectTests(unittest.TestCase):
    def test_inventory_sender_is_fixed_and_vip_is_still_the_default(self):
        task = email.task_identity.inventory.TASK_ID
        data = sent()
        with self.assertRaises(ResourceError): email.verify_effect("send_email", ARGS, OK, empty(), data, task_id=task)
        data["email"]["messages"][0]["from_addr"] = email.task_identity.EmailTaskIdentity(task).sender
        email.verify_effect("send_email", ARGS, OK, empty(), data, task_id=task)
        with self.assertRaises(ResourceError): email.verify_effect("send_email", ARGS, OK, empty(), data)

    def test_unknown_task_or_arbitrary_sender_is_rejected_before_ownership(self):
        cluster = Mock()
        with self.assertRaises(ValueError): email.EmailComponents(Path.cwd(), cluster, {}, task_id="foreign")
        with self.assertRaises(TypeError): email.EmailComponents(Path.cwd(), cluster, {}, sender="other@example.com")
        cluster.require_owner.assert_not_called()

    def test_inventory_initialization_delegates_to_its_original_preprocess_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); profile = EpisodeProfile(root, str(uuid4()), 0)
            cluster = Mock(profile=profile, started=True)
            original = {"products": [], "events": [], "email": {k: [] for k in email.TABLES}}
            with patch('integrations.inventory_data.initialize', return_value=original) as init, patch.object(email, 'public_blob') as vip:
                result = email.initialize(root, cluster, task_id=email.task_identity.inventory.TASK_ID)
            init.assert_called_once_with(root, cluster); vip.assert_not_called()
            self.assertEqual(result["email"], original["email"])
            self.assertEqual(result["office"]["events"], [])

    def test_runtime_and_build_sources_cover_task_identity_dependencies(self):
        from integrations.email_build import BUILD_INPUTS
        from integrations.runtime_identity import BRIDGE_FILES
        from integrations.task_profiles import VIP_IMPLEMENTATION_FILES
        for name in ('integrations/email_task_identity.py', 'integrations/inventory_data.py',
                     'training/inventory_task_review.py', 'adapters/inventory_alert_validation.py'):
            for closure in (BUILD_INPUTS, BRIDGE_FILES, VIP_IMPLEMENTATION_FILES): self.assertIn(name, closure)

    def test_original_send_requires_message_and_log(self):
        email.verify_effect("send_email", ARGS, OK, empty(), sent())
        partial = sent(); partial["email"]["sent_log"] = []
        with self.assertRaises(ResourceError): email.verify_effect("send_email", ARGS, OK, empty(), partial)

    def test_success_text_cannot_hide_wrong_recipient_or_source_mutation(self):
        data = sent(); data["email"]["messages"][0]["to_addr"] = ["other@example.com"]
        with self.assertRaises(ResourceError): email.verify_effect("send_email", ARGS, OK, empty(), data)
        data = sent(); data["office"]["cells"] = ["unexpected"]
        with self.assertRaises(ResourceError): email.verify_effect("send_email", ARGS, OK, empty(), data)

    def test_original_error_text_is_unknown_even_without_mcp_error_flag(self):
        with self.assertRaises(ResourceError):
            email.verify_effect("send_email", ARGS, response("Error sending email: fixture"), empty(), sent())

    def test_list_allows_only_exact_original_counter_refresh(self):
        before = sent(); after = deepcopy(before)
        after["email"]["folders"][0].update(message_count=1, unread_count=1)
        email.verify_effect("get_emails", {}, response("Folder: Sent\nPage: 1/1"), before, after)
        for field, value in (("message_count", 2), ("unread_count", 0), ("name", "INBOX")):
            wrong = deepcopy(after); wrong["email"]["folders"][0][field] = value
            with self.assertRaises(ResourceError): email.verify_effect("get_emails", {}, response("Folder: Sent\n"), before, wrong)

    def test_list_can_create_empty_sent_but_cannot_change_messages(self):
        after = empty(); after["email"]["folders"] = [{"id": 1, "name": "Sent", "message_count": 0, "unread_count": 0}]
        email.verify_effect("get_emails", {}, response("Folder 'Sent' is empty or page 1 is out of range"), empty(), after)
        with self.assertRaises(ResourceError): email.verify_effect("get_emails", {}, response("Folder: Sent\n"), empty(), sent())

    def test_read_requires_only_target_marked_and_preserves_body(self):
        before = sent(); after = deepcopy(before); after["email"]["messages"][0]["is_read"] = True
        email.verify_effect("read_email", {"email_id": "1"}, response("Email ID: 1\nText Content:\nHello"), before, after)
        after["email"]["messages"][0]["body_text"] = "altered"
        with self.assertRaises(ResourceError): email.verify_effect("read_email", {"email_id": "1"}, response("Email ID: 1\n"), before, after)

    def test_argument_boundary_blocks_indirect_file_and_header_operations(self):
        for name, args in (("delete_email", {}), ("send_email", {**ARGS, "attachments": ["/private"]}),
            ("send_email", {**ARGS, "to": "a@example.com,b@example.com"}), ("send_email", {**ARGS, "subject": "ok\r\nBcc: bad"}),
            ("read_email", {"email_id": "1 OR 1=1"}), ("get_emails", {"folder": "Sent", "page": True, "page_size": 20})):
            with self.subTest(name=name, args=args), self.assertRaises(ValueError): email.validate_arguments(name, args)

    def test_pg_aliases_and_indirect_overrides_must_all_bind_to_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = EpisodeProfile(Path(temp), str(uuid4()), 0); env = profile.environment({})
            require_pg_environment(profile, env)
            for key in ("PGHOST", "PG_HOST", "PGSERVICE", "PGHOSTADDR", "PGOPTIONS"):
                wrong = {**env, key: "foreign"}
                with self.subTest(key=key), self.assertRaises(ValueError): require_pg_environment(profile, wrong)

    def test_original_tool_decorator_registers_only_three_methods(self):
        server = Mock(); server.tool.return_value = lambda function: function
        registrar = ToolRegistrar(server)
        for name in (*email.METHODS, "delete_email", "download_attachment"):
            function = lambda: None; function.__name__ = name
            self.assertIs(registrar.tool()(function), function)
        self.assertEqual(server.tool.call_count, 3)


class EmailRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = EpisodeProfile(self.root, str(uuid4()), 0)
        self.cluster = Mock(profile=self.profile, started=True)
        self.component = email.EmailComponents(self.root, self.cluster, {})
        self.component.run.mkdir(parents=True)
        self.component.opened = True
        self.component.requests = {"schema_version": "owned-email-request-journal-v1", "profile": self.profile.record(), "entries": {}}
        self.component.launch = {"fixture": "launch"}
        self.component.observed = {"fixture": "process"}
        save(self.component.run / "launch.json", self.component.launch)
        self.identity_patch = patch.object(email.task_identity, "validate_process")
        self.identity_patch.start(); self.addCleanup(self.identity_patch.stop)
        save(self.component.run / "requests.json", self.component.requests)
        save(self.profile.base / "email-initialized.json", {"state": "ready", "task_id": email.TASK_ID, "profile": self.profile.record()})
        self.build_patch = patch.object(email, "verify_build"); self.build_patch.start(); self.addCleanup(self.build_patch.stop)
        self.component.client = Mock()

    async def test_intent_is_on_disk_before_rpc_and_replay_never_calls_again(self):
        rid = str(uuid4())
        async def invoke(*_):
            journal = json.loads((self.component.run / "requests.json").read_text())
            self.assertEqual(journal["entries"][rid]["state"], "prepared")
            return Mock(model_dump=lambda **kw: deepcopy(OK))
        self.component.client.call_tool = AsyncMock(side_effect=invoke)
        with patch.object(email, "snapshot", side_effect=[empty(), sent()]):
            result = await self.component.call("send_email", ARGS, rid)
            result["content"][0]["text"] = "caller mutated"
            cached = await self.component.call("send_email", ARGS, rid)
            self.assertEqual(cached, OK)
            with self.assertRaises(ValueError): await self.component.call("send_email", {**ARGS, "body": "conflict"}, rid)
        self.component.client.call_tool.assert_awaited_once()

    async def test_partial_send_is_durably_unknown_and_blocks_all_replay(self):
        rid = str(uuid4()); partial = sent(); partial["email"]["sent_log"] = []
        self.component.client.call_tool = AsyncMock(return_value=Mock(model_dump=lambda **kw: OK))
        with patch.object(email, "snapshot", side_effect=[empty(), partial]):
            with self.assertRaises(ResourceError): await self.component.call("send_email", ARGS, rid)
        saved = json.loads((self.component.run / "requests.json").read_text())
        self.assertEqual(saved["entries"][rid]["state"], "unknown")
        self.assertEqual(saved["entries"][rid]["response"], OK)
        for identity in (rid, str(uuid4())):
            with self.assertRaises(ResourceError): await self.component.call("send_email", ARGS, identity)
        self.component.client.call_tool.assert_awaited_once()
        with self.assertRaisesRegex(ResourceError, "reset refused"): email.initialize(self.root, self.cluster)

    async def test_observation_failure_keeps_original_response_and_unknown_intent(self):
        rid = str(uuid4()); original = response("Error sending email: missing log table")
        self.component.client.call_tool = AsyncMock(return_value=Mock(model_dump=lambda **kw: original))
        with patch.object(email, "snapshot", side_effect=[empty(), RuntimeError("snapshot unavailable")]):
            with self.assertRaises(ResourceError): await self.component.call("send_email", ARGS, rid)
        saved = json.loads((self.component.run / "requests.json").read_text())["entries"][rid]
        self.assertEqual(saved["response"], original); self.assertEqual(saved["state"], "unknown")

    async def test_cancel_after_rpc_started_is_unknown_and_not_retryable(self):
        rid = str(uuid4()); self.component.client.call_tool = AsyncMock(side_effect=asyncio.CancelledError())
        with patch.object(email, "snapshot", return_value=empty()):
            with self.assertRaises(ResourceError): await self.component.call("send_email", ARGS, rid)
        self.assertTrue(self.component.unknown)
        self.assertEqual(json.loads((self.component.run / "requests.json").read_text())["entries"][rid]["state"], "unknown")

    async def test_tampered_journal_or_owner_is_refused_before_rpc(self):
        self.component.client.call_tool = AsyncMock()
        save(self.component.run / "requests.json", {"entries": {}})
        with self.assertRaises(ResourceError): await self.component.call("send_email", ARGS, str(uuid4()))
        self.component.client.call_tool.assert_not_awaited()

    async def test_invalid_arguments_do_not_create_intent(self):
        self.component.client.call_tool = AsyncMock()
        with self.assertRaises(ValueError): await self.component.call("send_email", {**ARGS, "bcc": ["x@example.com"]}, str(uuid4()))
        self.assertFalse(self.component.requests["entries"])
        self.component.client.call_tool.assert_not_awaited()

    async def test_changed_launch_is_refused_before_rpc_and_preserves_journal(self):
        self.component.client.call_tool = AsyncMock()
        before = (self.component.run / 'requests.json').read_bytes()
        save(self.component.run / 'launch.json', {'foreign': 'launch'})
        with self.assertRaisesRegex(ResourceError, 'launch record changed'):
            await self.component.call('send_email', ARGS, str(uuid4()))
        self.component.client.call_tool.assert_not_awaited()
        self.assertEqual((self.component.run / 'requests.json').read_bytes(), before)

    async def test_live_task_process_binding_is_checked_again_for_cached_requests(self):
        self.component.client.call_tool = AsyncMock(return_value=Mock(model_dump=lambda **kw: deepcopy(OK)))
        rid = str(uuid4())
        with patch.object(email, 'snapshot', side_effect=[empty(), sent()]): await self.component.call('send_email', ARGS, rid)
        with patch.object(email.task_identity, 'validate_process', side_effect=ResourceError('PID was reused')):
            with self.assertRaisesRegex(ResourceError, 'PID was reused'): await self.component.call('send_email', ARGS, rid)
        self.component.client.call_tool.assert_awaited_once()

    async def test_prepared_intent_blocks_new_request_even_without_unknown_flag(self):
        self.component.requests["entries"][str(uuid4())] = {"state": "prepared"}
        save(self.component.run / "requests.json", self.component.requests)
        self.component.client.call_tool = AsyncMock()
        with self.assertRaisesRegex(ResourceError, "unresolved"): await self.component.call("send_email", ARGS, str(uuid4()))
        self.component.client.call_tool.assert_not_awaited()
