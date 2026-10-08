"""MCP stdio transport. Requires prior real-user task admission via local control."""
import asyncio
import json
import os
import sys
import copy
from .service import TOOL_NAMES, handle
from .resource_loader import load_json


async def dispatch(request):
    method = request.get("method")
    if method == "initialize":
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "ai-secretary", "version": "0.1.0"}}
    if method == "tools/list":
        catalog = load_json("tool-schemas.json")
        return {"tools": [{**entry, "description": "Local secretary tool; task-bound and sender approval enforced by worker"} for entry in catalog]}
    if method == "tools/call":
        params = request["params"]
        result = await handle({"tool": params["name"], "args": params.get("arguments", {}), "session": os.environ.get("SECRETARY_SESSION", "agent:main:secretary")})
        rendered = copy.deepcopy(result)
        screenshot = rendered.get("result", {}).get("screenshot", {})
        image = screenshot.pop("base64", None)
        content = [{"type": "text", "text": json.dumps(rendered, ensure_ascii=False)}]
        if image: content.append({"type": "image", "mimeType": "image/png", "data": image})
        return {"content": content, "isError": False}
    if method == "ping": return {}
    raise ValueError("unsupported MCP method")


def main():
    for line in sys.stdin.buffer:
        request = None
        try:
            if len(line) > 8 * 1024 * 1024: raise ValueError("MCP request too large")
            request = json.loads(line)
            if "id" not in request: continue
            result = asyncio.run(dispatch(request))
            reply = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        except Exception as error:
            reply = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None, "error": {"code": -32000, "message": str(error)}}
        print(json.dumps(reply, ensure_ascii=False), flush=True)

if __name__ == "__main__": main()
