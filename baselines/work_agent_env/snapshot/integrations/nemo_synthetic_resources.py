"""NeMo resource extension for explicitly synthetic, non-training diagnostics."""
import os
from typing import Literal

from aiohttp import ClientTimeout
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from nemo_gym.base_resources_server import (
    ResourcesCloseSessionRequest, ResourcesCloseSessionResponse,
    ResourcesSeedSessionRequest, ResourcesSeedSessionResponse, ResourcesVerifyRequest,
    SimpleResourcesServer,
)
from nemo_gym.episode_types import BaseEpisodeResponse, EpisodeId
from nemo_gym.server_utils import request, get_response_json


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resources_session_id: str
    episode_id: EpisodeId


class SyntheticToolInput(SessionInput):
    request_id: str
    name: str = Field(min_length=1, max_length=100)
    arguments: dict


class VerificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resources_session_id: str


class SyntheticMeasurement(BaseModel):
    reward: float | None
    mask_sample: bool
    source: Literal["project_synthetic"] = "project_synthetic"
    training_eligible: Literal[False] = False
    reward_version: str = "project-synthetic-diagnostic-v1"
    evaluation: dict | None = None
    evaluation_ref: str | None = None
    run_ref: str | None = None
    failure_kind: str | None = None


class SyntheticResourcesServer(SimpleResourcesServer):
    def get_session_middleware_key(self):
        return os.environ["WORKENV_SESSION_SECRET"]

    async def bridge(self, route, body):
        response = await request(
            "POST", os.environ.get("WORKENV_BRIDGE_URL", "http://10.0.2.2:18701") + "/synthetic" + route, json=body,
            headers={"Authorization": "Bearer " + os.environ["WORKENV_BRIDGE_TOKEN"]},
            timeout=ClientTimeout(total=60), _max_num_tries=1, _max_connection_retries=0,
            _resend_after_disconnect=False, _server_name="project_synthetic_resource_bridge")
        data = await get_response_json(response)
        if response.status != 200:
            raise HTTPException(response.status, str(data.get("error", "resource failure"))[:300])
        return data

    def bound(self, incoming, sid):
        if incoming.session.get("synthetic_session") != sid:
            raise HTTPException(409, "Seed the synthetic resource with this NeMo cookie")

    def setup_webserver(self):
        app = super().setup_webserver()
        app.post("/tool")(self.tool)
        app.post("/tools")(self.tools)
        return app

    async def seed_session(self, body: ResourcesSeedSessionRequest, request: Request) -> ResourcesSeedSessionResponse:
        result = await self.bridge("/seed_session", body.model_dump(mode="json"))
        request.session["synthetic_session"] = body.resources_session_id
        request.session["synthetic_task"] = body.task_id.model_dump(mode="json")
        return ResourcesSeedSessionResponse.model_validate(result)

    async def close_resources_session(self, body: ResourcesCloseSessionRequest) -> ResourcesCloseSessionResponse:
        return ResourcesCloseSessionResponse.model_validate(await self.bridge("/close_session", body.model_dump(mode="json")))

    async def tools(self, body: SessionInput, request: Request) -> dict:
        self.bound(request, body.resources_session_id)
        return await self.bridge("/tools", body.model_dump(mode="json"))

    async def tool(self, body: SyntheticToolInput, request: Request) -> dict:
        self.bound(request, body.resources_session_id)
        return await self.bridge("/tool", body.model_dump(mode="json"))

    async def verify(self, body: ResourcesVerifyRequest[VerificationInput], request: Request) -> BaseEpisodeResponse[SyntheticMeasurement]:
        self.bound(request, body.verification_input.resources_session_id)
        if request.session.get("synthetic_task") != body.task_id.model_dump(mode="json"):
            raise HTTPException(409, "Verification task differs from the seeded synthetic task")
        try:
            result = await self.bridge("/verify", {
                "resources_session_id": body.verification_input.resources_session_id,
                "episode_id": body.episode_id.model_dump(mode="json")})
            measurement = SyntheticMeasurement.model_validate(result)
        except Exception:
            measurement = SyntheticMeasurement(reward=None, mask_sample=True, failure_kind="project_synthetic:resource_unavailable")
        return BaseEpisodeResponse[SyntheticMeasurement](episode_id=body.episode_id, task_id=body.task_id, result=measurement)
