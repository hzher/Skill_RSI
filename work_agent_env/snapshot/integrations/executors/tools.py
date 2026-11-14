"""Search large installed catalogs while retaining task-owned tool dispatch."""
from copy import deepcopy


class ToolDispatcher:
    def __init__(self, tools, call):
        self.tools, self.invoke = deepcopy(tools), call
        self.lookup = {tool["name"]: tool for tool in self.tools}
        self.searchable = len(tools) > 80

    def definitions(self):
        if not self.searchable:
            return deepcopy(self.tools)
        return [{"name": "search_available_tools", "description": "Search installed task tools and inspect their exact parameter schemas. Empty query browses the catalog.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, "additionalProperties": False}},
            {"name": "call_available_tool", "description": "Call an installed task tool by the exact name and parameters returned by search_available_tools.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}},
                "required": ["name", "arguments"], "additionalProperties": False}}]

    async def call(self, name, arguments):
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        if self.searchable and name == "search_available_tools":
            query, offset, limit = arguments.get("query", ""), arguments.get("offset", 0), arguments.get("limit", 10)
            if set(arguments) - {"query", "offset", "limit"} or not isinstance(query, str) or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
                raise ValueError("Invalid tool search")
            tokens = query.lower().split()
            rows = [tool for tool in self.tools if all(token in (tool["name"] + " " + tool.get("description", "")).lower() for token in tokens)]
            return {"total": len(rows), "offset": offset, "tools": deepcopy(rows[offset:offset + limit])}
        if self.searchable:
            if name != "call_available_tool" or set(arguments) != {"name", "arguments"}:
                raise ValueError("Search for a task tool before calling it")
            name, arguments = arguments["name"], arguments["arguments"]
        if not isinstance(name, str) or name not in self.lookup:
            raise ValueError("Unavailable task tool")
        return await self.invoke(name, arguments)
