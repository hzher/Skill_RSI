"""Exercise actual worker transport dispatch and episode-bound artifact access."""
import io
import json
import types
import unittest
from unittest.mock import AsyncMock, patch
import uuid

try:
    from integrations import episode_worker as worker
except ImportError:
    worker = None


@unittest.skipIf(worker is None, "Worker import requires original-task optional dependencies")
class WorkerArtifactRoutes(unittest.IsolatedAsyncioTestCase):
    async def test_report_dispatch_is_bound_to_the_owned_resource(self):
        sid = str(uuid.uuid4())
        frames = [{"id": str(uuid.uuid4()), "route": "/write_report", "body": {"resources_session_id": sid}},
                  {"id": str(uuid.uuid4()), "route": "/write_report", "body": {"resources_session_id": str(uuid.uuid4())}},
                  {"id": str(uuid.uuid4()), "route": "/arbitrary", "body": {"resources_session_id": sid}}]
        stream = io.BytesIO(b"".join(json.dumps(frame).encode() + b"\n" for frame in frames))
        output = io.BytesIO()
        resources = types.SimpleNamespace(active=None, dispatch=AsyncMock(return_value={"written": True}))
        with patch.object(worker.sys, "stdin", types.SimpleNamespace(buffer=stream)), patch.object(worker.sys, "stdout", types.SimpleNamespace(buffer=output)):
            await worker.serve(resources, sid)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([reply["status"] for reply in replies], [200, 400, 400])
        resources.dispatch.assert_awaited_once_with("/write_report", frames[0]["body"])
