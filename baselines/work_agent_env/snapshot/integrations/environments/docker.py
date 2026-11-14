"""Owned per-task containers, optional resource services and model-selected tools."""
import asyncio
from contextlib import AsyncExitStack
from copy import deepcopy
import json
from pathlib import Path
import shutil
import uuid

from .spec import asset, validate_runtime


class DockerEnvironment:
    def __init__(self, *, task, package, directory, images=None):
        validate_runtime(task, package)
        self.task, self.package, self.directory = task, Path(package), Path(directory)
        self.config = task["environment"]["config"]
        self.timeout = self.config.get("timeout", 180)
        self.workspace = self.directory / "workspace"
        self.prefix = "bench-" + uuid.uuid4().hex
        self.container, self.network = self.prefix + "-agent", self.prefix + "-net"
        self.containers, self.tool_map, self.schemas = [], {}, []
        self.stack = AsyncExitStack()
        self.closed = None
        self.image = None
        self.network_created = False
        self.service_images = {}
        self.images = images or {}

    async def command(self, *args, stdin=None, timeout=None, check=True):
        process = await asyncio.create_subprocess_exec("docker", *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout or self.timeout)
            if check and process.returncode:
                # Receipts record a bounded local error, not environment variable values.
                raise RuntimeError("Docker operation failed: " + stderr.decode(errors="replace")[-1500:])
            return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    def mounts(self, declarations):
        result = []
        for mount in declarations:
            result += ["--mount", "type=bind,src=" + str(asset(self.package, mount["source"])) + ",dst=" + mount["target"] + ",readonly"]
        return result

    def environment_env(self):
        result = [part for key, value in self.config.get("env", {}).items() for part in ("-e", key + "=" + str(value))]
        for key in self.config.get("inherit_env", []):
            result += ["-e", key]
        return result

    async def initialize(self):
        self.workspace.mkdir(parents=True, exist_ok=False)
        if "initial_workspace" in self.config:
            shutil.copytree(asset(self.package, self.config["initial_workspace"]), self.workspace, dirs_exist_ok=True)
        if self.config["image"] in self.images:
            self.image = self.images[self.config["image"]]
        else:
            _, image, _ = await self.command("image", "inspect", "--format", "{{.Id}}", self.config["image"])
            self.image = image.strip()
        self.network_created = True
        from .network import create_network
        self.subnet = await asyncio.to_thread(create_network, self.network,
            internal=not self.config.get("network_access", False), timeout=self.timeout)
        for index, service in enumerate(self.config.get("services", [])):
            name = self.prefix + "-service-" + str(index)
            self.containers.append(name)
            env = [part for key, value in service.get("env", {}).items() for part in ("-e", key + "=" + str(value))]
            identity = self.images.get(service["image"])
            if identity is None:
                _, identity, _ = await self.command("image", "inspect", "--format", "{{.Id}}", service["image"])
            self.service_images[service["name"]] = identity.strip()
            await self.command("run", "-d", "--name", name, "--network", self.network,
                "--network-alias", service["name"], *env, *self.mounts(service.get("mounts", [])), identity.strip())
            if "ready" in service:
                deadline = asyncio.get_running_loop().time() + self.timeout
                while True:
                    code, _, _ = await self.command("exec", name, *service["ready"], check=False)
                    if not code:
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError("Resource service readiness timeout")
                    await asyncio.sleep(.5)
        self.containers.append(self.container)
        await self.command("run", "-d", "--name", self.container, "--network", self.network,
            "--mount", "type=bind,src=" + str(self.workspace.resolve()) + ",dst=/workspace",
            *self.mounts(self.config.get("mounts", [])), *self.environment_env(), "-w", "/workspace", "--entrypoint", "/bin/sh",
            self.image, "-c", "exec sleep infinity")
        if "preprocess" in self.config:
            await self.command("exec", self.container, *self.config["preprocess"])
        for tool in self.config.get("tools", []):
            self.tool_map[tool["name"]] = ("command", tool["argv"])
            self.schemas.append({key: deepcopy(tool[key]) for key in ("name", "description", "parameters")})
        if self.config.get("mcp_servers"):
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            for server in self.config["mcp_servers"]:
                transport = await self.stack.enter_async_context(stdio_client(StdioServerParameters(
                    command="docker", args=["exec", "-i", self.container, *server["argv"]])))
                session = await self.stack.enter_async_context(ClientSession(*transport))
                await asyncio.wait_for(session.initialize(), self.timeout)
                cursor = None
                while True:
                    tools = await asyncio.wait_for(session.list_tools(cursor=cursor), self.timeout)
                    for tool in tools.tools:
                        name = server["name"] + "__" + tool.name
                        if name in self.tool_map:
                            raise ValueError("Duplicate discovered tool name")
                        self.tool_map[name] = ("mcp", (session, tool.name))
                        self.schemas.append({"name": name, "description": tool.description or name, "parameters": tool.inputSchema})
                    cursor = tools.nextCursor
                    if not cursor:
                        break
        if not self.schemas:
            raise ValueError("Task environment provides no model tools")
        for requirement in self.task["tools"]:
            if requirement["kind"] == "mcp_server":
                found = requirement["name"] in {server["name"] for server in self.config.get("mcp_servers", [])}
            else:
                found = requirement["name"] in self.tool_map
            if not found:
                raise ValueError("Required tool is unavailable: " + requirement["name"])
        return {"container": self.container, "network": self.network, "network_subnet": self.subnet, "image_id": self.image,
            "service_images": self.service_images, "tools": self.discover(), "workspace": str(self.workspace)}

    def discover(self):
        return deepcopy(self.schemas)

    async def call(self, name, arguments):
        if name not in self.tool_map or not isinstance(arguments, dict):
            raise ValueError("Unknown tool or invalid arguments")
        from jsonschema import Draft202012Validator
        schema = next(row for row in self.schemas if row["name"] == name)
        errors = list(Draft202012Validator(schema["parameters"]).iter_errors(arguments))
        if errors:
            from integrations.executors.diagnostics import redact
            raise ValueError("Invalid tool arguments: " + "; ".join(
                "/".join(map(str, error.absolute_path)) + ": " + redact(error.message, 350) for error in errors[:3]))
        kind, handler = self.tool_map[name]
        if kind == "mcp":
            session, original_name = handler
            return (await asyncio.wait_for(session.call_tool(original_name, arguments), self.timeout)).model_dump(mode="json")
        code, output, error = await self.command("exec", "-i", self.container, *handler,
            stdin=json.dumps(arguments, allow_nan=False).encode(), check=False)
        return {"exit_code": code, "stdout": output, "stderr": error}

    async def evaluate(self):
        from integrations.benchmark_tasks import strict_json
        config = self.task["evaluation"]["config"]
        name = self.prefix + "-evaluator"
        self.containers.append(name)
        request = {"task": self.task, "context": {"workspace": "/workspace", "network": self.network}}
        _, output, _ = await self.command("run", "--rm", "-i", "--name", name,
            "--network", self.network, "--mount", "type=bind,src=" + str(self.workspace.resolve()) + ",dst=/workspace,readonly",
            *self.mounts(config.get("mounts", [])), *self.environment_env(), "-w", "/workspace", "--entrypoint", config["argv"][0],
            self.image, *config["argv"][1:], stdin=json.dumps(request, allow_nan=False).encode(), timeout=config.get("timeout", 180))
        return strict_json(output)

    async def close(self):
        if self.closed is not None:
            return self.closed
        errors = []
        try:
            async with asyncio.timeout(20):
                await self.stack.aclose()
        except BaseException as exc:
            errors.append(type(exc).__name__)
        for name in reversed(self.containers):
            try:
                # A successful inspect after removal would mean cleanup is incomplete.
                await self.command("rm", "-fv", name, check=False, timeout=20)
                code, _, message = await self.command("container", "inspect", name, check=False, timeout=20)
                if code == 0 or "no such container" not in message.lower():
                    errors.append("container_removal_unconfirmed")
            except Exception as exc:
                errors.append(type(exc).__name__)
        if self.network_created:
            try:
                code, _, message = await self.command("network", "rm", self.network, check=False, timeout=20)
                if code and "not found" not in message.lower() and "no such network" not in message.lower():
                    errors.append("network_cleanup_failed")
            except Exception as exc:
                errors.append(type(exc).__name__)
        self.closed = {"resources_closed": not errors, "errors": errors}
        return self.closed
