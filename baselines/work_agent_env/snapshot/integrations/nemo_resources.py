"""Actual NeMo Gym Resources Server; run inside the owned Linux VM.

The Windows resource bridge only hosts the original task's MCP dependencies.
NeMo owns the public seed/tool/verify/close endpoints and session middleware.
"""
import importlib.metadata
import json
import hashlib
from pathlib import Path
import os
import platform
from typing import Literal, ClassVar

from aiohttp import ClientTimeout
from fastapi import HTTPException, Request
from omegaconf import OmegaConf
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

from nemo_gym.base_resources_server import (
    BaseResourcesServerConfig, ResourcesCloseSessionRequest, ResourcesCloseSessionResponse,
    ResourcesSeedSessionRequest, ResourcesSeedSessionResponse, ResourcesVerifyRequest,
    SimpleResourcesServer,
)
from nemo_gym.config_types import BaseServerConfig
from nemo_gym.episode_types import BaseEpisodeResponse, EpisodeId
from nemo_gym.server_utils import ServerClient, get_response_json, request
from integrations import nemo_synthetic_resources
from integrations.nemo_synthetic_resources import SyntheticResourcesServer
from integrations.verifiers.nemo import evaluate_for_nemo

LOADED_ADAPTER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
LOADED_EXTENSION_SHA256 = {"integrations/nemo_synthetic_resources.py": hashlib.sha256(
    Path(nemo_synthetic_resources.__file__).read_bytes()).hexdigest()}
LOADED_EXTENSION_SHA256.update({"integrations/verifiers/" + path.name: hashlib.sha256(path.read_bytes()).hexdigest()
    for path in (Path(__file__).parent / "verifiers").glob("*.py")})


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resources_session_id: str
    episode_id: EpisodeId
    request_id: str
    server: Literal["woocommerce", "google_sheet", "howtocook", "google_calendar", "email"]
    name: str = Field(min_length=1, max_length=100)
    arguments: dict


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resources_session_id: str
    episode_id: EpisodeId


class ReportRequest(SessionRequest):
    request_id: str
    base64: str = Field(min_length=1, max_length=2_800_000)


class VerificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resources_session_id: str


class WorkbookRequest(SessionRequest):
    request_id: str
    workbook: dict


class Measurement(BaseModel):
    reward: float | None
    mask_sample: bool
    evaluation: dict | None = None
    reward_version: str = "toolathlon-original-and-supplemental-v1"
    failure_kind: str | None = None
    failure_reason: str | None = None
    run_ref: str | None = None
    evaluation_ref: str | None = None


class ToolathlonResources(SimpleResourcesServer):
    session_prefix: ClassVar[str] = "toolathlon"
    bridge_prefix: ClassVar[str] = ""

    def get_session_middleware_key(self):
        return os.environ["WORKENV_SESSION_SECRET"]

    async def bridge(self, route, body):
        response = await request(
            "POST", os.environ.get("WORKENV_BRIDGE_URL", "http://10.0.2.2:18701") + self.bridge_prefix + route,
            json=body, headers={"Authorization": "Bearer " + os.environ["WORKENV_BRIDGE_TOKEN"]},
            timeout=ClientTimeout(total=180), _max_num_tries=1, _max_connection_retries=0,
            _resend_after_disconnect=False, _server_name="toolathlon_native_bridge",
        )
        data = await get_response_json(response)
        if response.status != 200:
            raise HTTPException(response.status, str(data.get("error", "resource failure"))[:300])
        return data

    def bound(self, incoming, sid):
        if incoming.session.get(self.session_prefix + "_session") != sid:
            raise HTTPException(409, "Seed the resource session with this NeMo cookie before using it")

    def setup_webserver(self):
        app = super().setup_webserver()
        app.post("/tool")(self.tool)
        app.post("/tools")(self.tools)
        app.post("/write_report")(self.write_report)
        app.post("/write_workbook")(self.write_workbook)
        app.post("/read_workbook")(self.read_workbook)
        app.get("/provenance")(self.provenance)
        self.setup_liveness(app)
        if not self.bridge_prefix:
            synthetic = SyntheticResourcesServer(
                config=self.config.model_copy(update={"name": "project_synthetic_resources"}), server_client=self.server_client)
            app.mount("/synthetic", synthetic.setup_webserver())
            isolated = IsolatedToolathlonResources(
                config=self.config.model_copy(update={"name": "isolated_toolathlon_resources"}), server_client=self.server_client)
            app.mount("/isolated", isolated.setup_webserver())
        return app

    async def provenance(self):
        return {"framework": "nemo_gym", "version": importlib.metadata.version("nemo-gym"),
                "source_commit": "98de01c6168062fb746d45f8cd56b38b5580555b",
                "python": platform.python_version(), "system": platform.system(),
                "resource_adapter": "toolathlon-native-v1", "loaded_adapter_sha256": LOADED_ADAPTER_SHA256,
                "resource_backend": "isolated" if self.bridge_prefix else "shared",
                "environment_version": "nemo-isolated-toolathlon-resources-v1" if self.bridge_prefix else "nemo-toolathlon-resources-v1",
                "loaded_extension_sha256": LOADED_EXTENSION_SHA256,
                "pid": os.getpid(), "real_model_calls": 0, "training_updates": 0}

    async def seed_session(self, body: ResourcesSeedSessionRequest, request: Request) -> ResourcesSeedSessionResponse:
        result = await self.bridge("/seed_session", body.model_dump(mode="json"))
        request.session[self.session_prefix + "_session"] = body.resources_session_id
        request.session[self.session_prefix + "_task"] = body.task_id.model_dump(mode="json")
        return ResourcesSeedSessionResponse.model_validate(result)

    async def close_resources_session(self, body: ResourcesCloseSessionRequest) -> ResourcesCloseSessionResponse:
        return ResourcesCloseSessionResponse.model_validate(await self.bridge("/close_session", body.model_dump(mode="json")))

    async def tool(self, body: ToolRequest, request: Request) -> dict:
        self.bound(request, body.resources_session_id)
        return await self.bridge("/tool", body.model_dump(mode="json"))

    async def tools(self, body: SessionRequest, request: Request) -> dict:
        self.bound(request, body.resources_session_id)
        return await self.bridge("/tools", body.model_dump(mode="json"))

    async def write_report(self, body: ReportRequest, request: Request) -> dict:
        self.bound(request, body.resources_session_id)
        return await self.bridge("/write_report", body.model_dump(mode="json"))

    async def write_workbook(self, body: WorkbookRequest, request: Request) -> dict:
        self.bound(request, body.resources_session_id)
        return await self.bridge("/write_workbook", body.model_dump(mode="json"))

    async def read_workbook(self, body: SessionRequest, request: Request) -> dict:
        self.bound(request, body.resources_session_id)
        return await self.bridge("/read_workbook", body.model_dump(mode="json"))

    async def verify(self, body: ResourcesVerifyRequest[VerificationInput], request: Request) -> BaseEpisodeResponse[Measurement]:
        self.bound(request, body.verification_input.resources_session_id)
        if request.session.get(self.session_prefix + "_task") != body.task_id.model_dump(mode="json"):
            raise HTTPException(409, "Verification task identity does not match the seeded task")
        resource_body = {"resources_session_id": body.verification_input.resources_session_id,
                         "episode_id": body.episode_id.model_dump(mode="json")}
        try:
            task_set_path = os.environ.get("WORKENV_VERIFIER_TASK_SET")
            if task_set_path:
                if body.task_id.taskset != os.environ["WORKENV_VERIFIER_TASKSET_NAME"]:
                    raise ValueError("NeMo taskset differs from verifier deployment")
                result = await evaluate_for_nemo(task_set_path=task_set_path,
                    verifier_config_path=os.environ["WORKENV_VERIFIER_CONFIG"],
                    task_id=body.task_id.task_id, context=resource_body)
            else:
                result = await self.bridge("/verify", resource_body)
            result.pop("resources_session_id", None)
            measurement = Measurement.model_validate(result)
        except Exception as exc:
            # Resource errors must never become an observed task failure/reward zero.
            measurement = Measurement(reward=None, mask_sample=True, failure_kind="toolathlon:resource_unavailable",
                                      failure_reason=type(exc).__name__, reward_version="unavailable")
        return BaseEpisodeResponse[Measurement](episode_id=body.episode_id, task_id=body.task_id, result=measurement)


class IsolatedToolathlonResources(ToolathlonResources):
    session_prefix: ClassVar[str] = "isolated_toolathlon"
    bridge_prefix: ClassVar[str] = "/isolated"


def main():
    for name in ("WORKENV_SESSION_SECRET", "WORKENV_BRIDGE_TOKEN"):
        if len(os.environ.get(name, "")) < 32:
            raise RuntimeError("Private runtime credentials missing: " + name)
    # Use the real NeMo launch path, including connection pools, cancellation,
    # session/verify middleware, startup timeline, limits and shutdown hooks.
    os.environ["NEMO_GYM_CONFIG_PATH"] = "toolathlon_resources"
    os.environ["NEMO_GYM_CONFIG_DICT"] = json.dumps({
        "head_server": {"host": "127.0.0.1", "port": 18703}, "dry_run": False,
        "toolathlon_resources": {"resources_servers": {"toolathlon_native": {
            "host": os.environ.get("WORKENV_NEMO_HOST", "0.0.0.0"), "port": 18700, "entrypoint": "nemo_resources.py"}}},
        "global_aiohttp_connector_limit": 32, "global_aiohttp_connector_limit_per_host": 8,
    })
    ToolathlonResources.run_webserver()


if __name__ == "__main__":
    main()
