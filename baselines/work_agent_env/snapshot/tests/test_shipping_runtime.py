"""Shipping route, seed and measurement boundaries before actual NeMo use."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from adapters.shipping_evaluation import episode_result, REWARD_VERSION, REFERENCE_MODE
from integrations.shipping_resource import ShippingResourceAdapter, METHODS
from integrations.shipping_components import configuration_digest
from integrations.episode_cluster import EpisodeCluster, EpisodeProfile
from integrations.task_profiles import IMPLEMENTATION_FILES, build_registry
from integrations.task_routing import TaskRouter
from integrations.isolated_gateway import IsolatedGateway
from workenv.core import ResourceError, save

ROOT = Path(__file__).resolve().parents[1]
TASK = "wc-shipping-zone-gsheet"


class ShippingProfileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for name in {*IMPLEMENTATION_FILES, "configs/mainline_sources.lock.json", "training/toolathlon_seed_capabilities.json"}:
            path = self.root / name; path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes((ROOT/name).read_bytes())
        self.registry = build_registry(self.root)
        save(self.root/"configs/task_adapter_profiles.json",self.registry)
        self.seed={"resources_session_id":"9d8c116b-0f7c-4c88-b690-4183972c37cd", "episode_id":{"rollout_id":"fixture","attempt":0},
                   "task_id":{"taskset":"toolathlon_gym:fixture","task_id":TASK},"task_data":{}}

    def test_isolated_profile_exposes_explicit_project_mode_and_no_admission(self):
        profile=self.registry["profiles"][TASK+"-v1"]
        self.assertEqual(profile["resource_backends"],["isolated"])
        route=TaskRouter(self.root,"isolated").authorize(self.seed)
        self.assertEqual(route["measurement_mode"],REFERENCE_MODE)
        self.assertEqual(route["reward_version"],REWARD_VERSION)
        self.assertIs(route["official_benchmark_score_available"],False)
        self.assertIs(route["training_admission"],False)
        self.assertEqual(route["mcp_methods"]["woocommerce"],sorted(METHODS["woocommerce"]))
        self.assertIs(profile["shipping_locations_enabled"],True)
        self.assertEqual(profile["project_read_extensions"],["workenv_shipping_zone_locations_get"])

    def test_shared_backend_refuses_task_before_resource_allocation(self):
        with self.assertRaises(ResourceError): TaskRouter(self.root,"shared").authorize(self.seed)
        self.assertFalse((self.root/".runtime").exists())

    def test_changed_shared_component_blocks_routing(self):
        router=TaskRouter(self.root,"isolated")
        path=self.root/"integrations/customer_report_components.py"
        path.write_bytes(path.read_bytes()+b"\n# changed fixture\n")
        with self.assertRaises(ResourceError):router.authorize(self.seed)

    def test_seed_rejects_foreign_task_sheet_rows_and_changed_bytes(self):
        raw=b"-- owned fixture wc plus empty sheets\n"
        name=".runtime/native-episodes/seed-cache/fixture.sql"
        path=self.root/name;path.parent.mkdir(parents=True);path.write_bytes(raw)
        profile=self.registry["profiles"][TASK+"-v1"]
        record={"task_commit":profile["source_commit"],"task_id":TASK,"schemas":["wc","gsheet"],"sheet_rows_included":False,
                "shipping_configuration_sha256":"a"*64, "task_config_sha256":profile["task_config_sha256"],"seed_ref":name,"sha256":hashlib.sha256(raw).hexdigest()}
        marker=self.root/".runtime/nemo-vm/shipping-seed.json"
        gateway=object.__new__(IsolatedGateway)
        with patch("integrations.isolated_gateway.ROOT",self.root):
            save(marker,record);self.assertEqual(gateway._seed_sql(TASK),path)
            for changed in ({**record,"task_id":"wc-order-analysis-gcal"},{**record,"sheet_rows_included":True},{**record,"task_config_sha256":"0"*64},{**record,"shipping_configuration_sha256":None}):
                save(marker,changed)
                with self.assertRaises(ResourceError):gateway._seed_sql(TASK)
            save(marker,record);path.write_bytes(b"changed")
            with self.assertRaises(ResourceError):gateway._seed_sql(TASK)


class ShippingMeasurementTests(unittest.TestCase):
    def component(self,passed):
        return {"component_measurement_passed":passed,"original_parser_passed":passed,"supplemental_passed":passed,"evaluation_ref":"fixture/result.json"}

    def test_known_success_failure_and_unknown_remain_distinct(self):
        for passed,status in ((True,"completed"),(False,"completed"),(None,"error"),(1,"error")):
            result=episode_result(self.component(passed))
            self.assertEqual(result["evaluation_status"],status)
            self.assertIs(result["task_passed"],passed if type(passed) is bool else None)
            self.assertEqual(result["reward_version"],REWARD_VERSION)
            self.assertEqual(result["reference_mode"],REFERENCE_MODE)
            self.assertIs(result["official_benchmark_score_available"],False)
            self.assertIs(result["training_eligible"],False)


class ShippingAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        profile=EpisodeProfile(self.root,"9d8c116b-0f7c-4c88-b690-4183972c37cd",0)
        profile.base.mkdir(parents=True)
        save(profile.base/"cluster_owner.json",profile.record())
        self.cluster=EpisodeCluster(profile);self.cluster.started=True
        self.adapter=ShippingResourceAdapter(self.root,self.cluster)

    async def test_run_cannot_reopen_or_accept_unallowlisted_mutation(self):
        self.adapter.new_run()
        with self.assertRaises(ResourceError):self.adapter.new_run()
        with self.assertRaises(ValueError):await self.adapter.call("woocommerce","woo_orders_delete",{"id":1},[])
        self.assertEqual(self.adapter.components.journal,[])

    async def test_failure_after_services_entered_closes_context(self):
        run=self.adapter.new_run()
        context=AsyncMock();self.adapter.components.opened=True
        self.adapter.components.schemas=[{"server":s,"name":n} for s,names in METHODS.items() for n in names]
        with patch.object(self.adapter.components,"open",return_value=context),patch("integrations.shipping_resource.snapshot",side_effect=ResourceError("fixture snapshot failure")) as read_snapshot:
            with self.assertRaises(ResourceError):await self.adapter.seed(run)
        read_snapshot.assert_called_once()
        context.__aexit__.assert_awaited_once()
        self.assertIsNone(self.adapter.context)

    async def test_foreign_run_close_is_rejected(self):
        self.adapter.new_run()
        with self.assertRaises(ResourceError):await self.adapter.close(self.root/"foreign")

    async def test_restored_shipping_configuration_must_match_seed(self):
        data={"shipping_zones":[{"id":1}],"shipping_zone_locations":[],"shipping_zone_methods":[]}
        marker=self.root/".runtime/nemo-vm/shipping-seed.json"
        save(marker,{"shipping_configuration_sha256":configuration_digest(data)})
        with patch("integrations.shipping_resource.snapshot",return_value=data):
            self.assertEqual(self.adapter.source_snapshot(),data)
            save(marker,{"shipping_configuration_sha256":"0"*64})
            with self.assertRaises(ResourceError):self.adapter.source_snapshot()

    async def test_verification_awaits_to_result_tuple(self):
        self.adapter.context=object();self.adapter.components.opened=True
        with patch("integrations.office_report_resource.artifacts.workspace_for"),patch("integrations.shipping_resource.evaluate_episode",return_value=({"task_passed":True},self.root/"fixture.json")):
            result=await self.adapter.verify()
        self.assertIsInstance(result,tuple)
        self.assertIs(result[0]["task_passed"],True)

    async def test_cleanup_failure_still_clears_context_and_propagates(self):
        run=self.adapter.new_run();context=AsyncMock();self.adapter.components.opened=True
        self.adapter.components.schemas=[{"server":s,"name":n} for s,names in METHODS.items() for n in names]
        context.__aexit__.side_effect=ResourceError("fixture closure unknown")
        with patch.object(self.adapter.components,"open",return_value=context),patch("integrations.shipping_resource.snapshot",side_effect=ResourceError("fixture snapshot failure")):
            with self.assertRaisesRegex(ResourceError,"closure unknown"):await self.adapter.seed(run)
        self.assertIsNone(self.adapter.context)

    async def test_workbook_schema_keeps_method_text_and_summary_numbers(self):
        self.adapter.context=object();self.adapter.components.opened=True
        with patch("integrations.office_report_resource.artifacts.workspace_for"):
            schema=self.adapter.tools()["artifact_tools"][0]["inputSchema"]
        zones=schema["properties"]["Zone Summary"]["items"]["properties"]
        methods=schema["properties"]["Methods"]["items"]["properties"]
        self.assertEqual(zones["Total_Revenue"]["type"],"number")
        self.assertTrue(all(c["type"]=="string" for c in methods.values()))
