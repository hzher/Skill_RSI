"""Fixed API model clients; all tool execution stays in the task environment."""
import os
import asyncio
from .diagnostics import error_details, request_summary


class APIModel:
    async def request(self, create, **kwargs):
        self.last_request = {"request": request_summary(kwargs), "attempts": []}
        for attempt in range(3):
            try:
                response = await create(**kwargs)
                self.last_request["attempts"].append({"accepted": True})
                return response
            except Exception as exc:
                detail = error_details(exc)
                self.last_request["attempts"].append({"accepted": False, **detail})
                status = getattr(exc, "status_code", None)
                transient = status == 429 or isinstance(status, int) and status >= 500 or type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}
                if not transient or attempt == 2:
                    exc.request_diagnostics = self.last_request
                    raise
                await asyncio.sleep(2 ** attempt)

    def __init__(self, options):
        self.options = options
        key = os.environ.get("MODEL_API_KEY")
        if not key:
            raise ValueError("MODEL_API_KEY is required")
        kwargs = {"api_key": key, "timeout": options.request_timeout, "max_retries": 0}
        if os.environ.get("MODEL_API_URL"):
            kwargs["base_url"] = os.environ["MODEL_API_URL"]
        if options.provider == "openai":
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(**kwargs)
        else:
            from anthropic import AsyncAnthropic
            self.client = AsyncAnthropic(**kwargs)

    async def turn(self, system, tools, history):
        if self.options.provider == "openai":
            messages = [{"role": "system", "content": system}]
            for event in history:
                if event["role"] == "assistant":
                    item = {"role": "assistant", "content": event["text"] or None}
                    if event["calls"]:
                        import json
                        item["tool_calls"] = [{"id": call["id"], "type": "function", "function": {
                            "name": call["name"], "arguments": json.dumps(call["arguments"])}} for call in event["calls"]]
                    messages.append(item)
                elif event["role"] == "tool":
                    messages.append({"role": "tool", "tool_call_id": event["id"], "content": event["content"]})
                else:
                    messages.append(event)
            response = await self.request(self.client.chat.completions.create, model=self.options.model, messages=messages,
                tools=[{"type": "function", "function": {**tool, "strict": False}} for tool in tools], tool_choice="auto",
                max_completion_tokens=self.options.max_output_tokens)
            choice = response.choices[0]
            self.last_request["response"] = {"model": response.model, "finish_reason": choice.finish_reason,
                "message": choice.message.model_dump(mode="json") if hasattr(choice.message, "model_dump") else None}
            if choice.finish_reason not in ("stop", "tool_calls"):
                raise RuntimeError("Model response did not complete")
            import json
            calls = [{"id": call.id, "name": call.function.name, "arguments": json.loads(call.function.arguments)}
                for call in choice.message.tool_calls or []]
            return {"text": choice.message.content or "", "calls": calls,
                "usage": response.usage.model_dump() if response.usage else None, "model": response.model,
                "provider_response_id": getattr(response, "id", None),
                "request_diagnostics": self.last_request}
        messages = []
        for event in history:
            if event["role"] == "assistant":
                blocks = ([{"type": "text", "text": event["text"]}] if event["text"] else []) + [
                    {"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["arguments"]} for call in event["calls"]]
                messages.append({"role": "assistant", "content": blocks})
            elif event["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": event["id"], "content": event["content"]}
                if messages and messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})
            else:
                messages.append(event)
        response = await self.request(self.client.messages.create, model=self.options.model, system=system, messages=messages,
            tools=[{"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]} for tool in tools],
            max_tokens=self.options.max_output_tokens)
        if response.stop_reason not in ("end_turn", "tool_use"):
            raise RuntimeError("Model response did not complete")
        return {"text": "".join(block.text for block in response.content if block.type == "text"),
            "calls": [{"id": block.id, "name": block.name, "arguments": block.input}
                for block in response.content if block.type == "tool_use"],
            "usage": response.usage.model_dump(), "model": response.model,
            "provider_response_id": getattr(response, "id", None), "request_diagnostics": self.last_request}

    async def close(self):
        await self.client.close()
