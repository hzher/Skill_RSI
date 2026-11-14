"""Async resource routing never leaks between tasks or back to the shared adapter."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from integrations.episode_cluster import EpisodeProfile
from workenv.core import save
try:
    from adapters import toolathlon_native as native
except ImportError:
    native = None


@unittest.skipIf(native is None, "Original task adapter requires psycopg2 and openpyxl")
class NativeEpisodeContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-context-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        item = patch.object(native, "ROOT", self.root)
        item.start()
        self.addCleanup(item.stop)
        self.profiles = [EpisodeProfile(self.root, str(uuid.uuid4()), slot) for slot in (0, 1)]
        for profile in self.profiles: save(profile.base / "cluster_owner.json", profile.record())

    async def test_interleaved_async_tasks_keep_their_own_service_routing(self):
        async def work(profile):
            with native.episode_resource(profile):
                before = native.env()
                await asyncio.sleep(0)
                after = native.env()
                self.assertEqual(before["PGHOST"], profile.host)
                self.assertEqual(after["PG_HOST"], profile.host)
                return after["WORKENV_WOOCOMMERCE_MODULE_ID"]
        results = await asyncio.gather(*(work(p) for p in self.profiles))
        self.assertEqual(results, [p.resource_id for p in self.profiles])
        self.assertEqual(native.env()["PGHOST"], "127.0.0.1")

    async def test_nested_context_and_exception_restore_the_prior_resource(self):
        with native.episode_resource(self.profiles[0]):
            with self.assertRaises(RuntimeError):
                with native.episode_resource(self.profiles[1]):
                    self.assertEqual(native.env()["PGHOST"], self.profiles[1].host)
                    raise RuntimeError("unit fault")
            self.assertEqual(native.env()["PGHOST"], self.profiles[0].host)
        self.assertEqual(native.env()["PGHOST"], "127.0.0.1")

    async def test_a_run_cannot_be_used_in_another_episode_or_shared_context(self):
        run = self.root / "runs/unit"
        save(run / "run.json", {"resource_profile": self.profiles[0].record()})
        with native.episode_resource(self.profiles[0]): native.require_run_profile(run)
        with native.episode_resource(self.profiles[1]):
            with self.assertRaises(ValueError): native.require_run_profile(run)
        with self.assertRaises(ValueError): native.require_run_profile(run)
