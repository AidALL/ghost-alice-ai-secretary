"""MCP stdio transport with a host-private, persistent worker capability.

The local operator admits the human task before this transport starts. The
stdio client can submit catalogued tool data, never intake or authority fields.
"""
import asyncio
import copy
import json
import os
import sys
import uuid

from .service import TOOL_NAMES, handle
from .resource_loader import load_json
from .worker_sessions import validate_worker_call

MAX_REQUEST = 8 * 1024 * 1024


def _arguments(name, arguments):
    snapshot = validate_worker_call(name, arguments, allowed_tools=TOOL_NAMES)
    entry = next(item for item in load_json("tool-schemas.json") if item["name"] == name)
    schema = entry["inputSchema"]
    if set(snapshot) - set(schema["properties"]):
        raise ValueError("Secretary arguments contain unsupported fields")
    if set(schema.get("required", ())) - set(snapshot):
        raise ValueError("Secretary required argument is missing")
    for key, value in snapshot.items():
        expected = schema["properties"][key]
        kind = expected["type"]
        valid = (type(value) is int if kind == "integer" else
                 type(value) in (int, float) if kind == "number" else
                 type(value) is dict if kind == "object" else
                 type(value) is list if kind == "array" else
                 type(value) is str if kind == "string" else
                 type(value) is bool if kind == "boolean" else False)
        if not valid or ("enum" in expected and value not in expected["enum"]):
            raise ValueError(f"Invalid Secretary argument: {key}")
    return snapshot


class RPCTransport:
    """One trusted stdio lifetime; closed transports cannot reopen authority."""

    def __init__(self, *, session=None):
        self._session = session or os.environ.get("SECRETARY_SESSION", "agent:main:secretary")
        self._transport_id = str(uuid.uuid4())
        self._binding = None
        self._closed = False
        self._lock = asyncio.Lock()

    async def _open(self):
        async with self._lock:
            if self._closed:
                raise PermissionError("Secretary host transport is closed")
            if self._binding is None:
                binding = await handle({"kind": "worker_open", "session": self._session,
                                        "transport_id": self._transport_id})
                if not binding.get("token") or binding.get("transport_id") != self._transport_id:
                    raise PermissionError("Secretary host binding is invalid")
                self._binding = binding
                if self._closed:
                    await self._close_binding()
                    raise PermissionError("Secretary host transport closed during admission")
            return self._binding

    async def _close_binding(self):
        if self._binding is None:
            return
        binding = self._binding
        drain = await handle({"kind": "worker_close", "session": self._session,
                              "transport_id": self._transport_id, "capability": binding["token"]})
        if drain.get("state") == "Stopped":
            self._binding = None
        return drain

    async def close(self):
        # Local revocation precedes awaits. Native/process quiescence is owned
        # by the durable registry, rather than inferred from EOF alone.
        self._closed = True
        async with self._lock:
            return await self._close_binding()

    async def dispatch(self, request):
        method = request.get("method")
        if method == "initialize":
            return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "ai-secretary", "version": "0.1.0"}}
        if method == "tools/list":
            catalog = load_json("tool-schemas.json")
            return {"tools": [{**entry, "description": "Local secretary tool; task-bound and sender approval enforced by worker"} for entry in catalog]}
        if method == "tools/call":
            params = request["params"]
            name = params["name"]
            arguments = _arguments(name, params.get("arguments", {}))
            binding = await self._open()
            if self._closed:
                raise PermissionError("Secretary host transport is closed")
            result = await handle({"kind": "call", "tool": name, "args": arguments,
                                   "session": self._session, "transport_id": self._transport_id,
                                   "capability": binding["token"]})
            if self._closed:
                raise PermissionError("Secretary host transport closed before result delivery")
            rendered = copy.deepcopy(result)
            screenshot = rendered.get("result", {}).get("screenshot", {})
            image = screenshot.pop("base64", None)
            content = [{"type": "text", "text": json.dumps(rendered, ensure_ascii=False)}]
            if image:
                content.append({"type": "image", "mimeType": "image/png", "data": image})
            return {"content": content, "isError": False}
        if method == "ping":
            return {}
        raise ValueError("unsupported MCP method")


async def dispatch(request):
    """Convenience for isolated in-process calls; stdio uses RPCTransport."""
    transport = RPCTransport()
    try:
        return await transport.dispatch(request)
    finally:
        await transport.close()


async def serve(input_stream=None, output_stream=None, *, session=None):
    input_stream = input_stream if input_stream is not None else sys.stdin.buffer
    output_stream = output_stream if output_stream is not None else sys.stdout
    transport = RPCTransport(session=session)
    pipe = None
    try:
        try:
            input_stream.fileno()
        except (AttributeError, OSError):
            async def readline():
                return input_stream.readline(MAX_REQUEST + 1)
        else:
            reader = asyncio.StreamReader(limit=MAX_REQUEST + 1)
            protocol = asyncio.StreamReaderProtocol(reader)
            pipe, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, input_stream)
            async def readline():
                return await reader.readline()
        while True:
            request = None
            line = await readline()
            if not line:
                break
            try:
                if len(line) > MAX_REQUEST:
                    raise ValueError("MCP request too large")
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("MCP request must be an object")
                if "id" not in request:
                    continue
                result = await transport.dispatch(request)
                reply = {"jsonrpc": "2.0", "id": request["id"], "result": result}
            except Exception as error:
                reply = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                         "error": {"code": -32000, "message": str(error)}}
            output_stream.write(json.dumps(reply, ensure_ascii=False) + "\n")
            output_stream.flush()
    finally:
        # A cancelled request must still revoke its capability and await drain.
        cleanup = asyncio.create_task(transport.close())
        interrupted = False
        try:
            while True:
                try:
                    drain = await asyncio.shield(cleanup)
                    if drain is not None and drain.get("state") != "Stopped":
                        raise RuntimeError("Secretary cleanup remains Draining; reconcile the durable lifecycle")
                    break
                except asyncio.CancelledError:
                    interrupted = True
            if interrupted:
                raise asyncio.CancelledError
        finally:
            if pipe is not None:
                pipe.close()


def main():
    asyncio.run(serve())


if __name__ == "__main__":
    main()
