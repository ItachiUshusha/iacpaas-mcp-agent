"""Exercise real MCP transports. This is a deterministic client, not an LLM agent."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
import jsonschema
import httpx

from domain import BASE
import iacpaas_client as api


@asynccontextmanager
async def connect(transport, mode, env):
    if transport == "stdio":
        params = StdioServerParameters(command=sys.executable,
                    args=["-X", "utf8", str(BASE / "server.py"), "--mode", mode], env=env, cwd=str(BASE))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
                yield session
        return
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([sys.executable, "-X", "utf8", str(BASE / "server.py"),
                                "--transport", "http", "--port", str(port), "--mode", mode],
                               cwd=BASE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 15
        while True:
            if process.poll() is not None:
                raise RuntimeError("HTTP server exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("HTTP server startup timed out") from None
                await asyncio.sleep(0.1)
        # Loopback traffic must not pass through a system HTTP proxy.
        async with httpx.AsyncClient(trust_env=False, timeout=60) as http_client:
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http_client) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
                    yield session
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


async def exercise(session, mode):
    await session.initialize()
    tools = (await session.list_tools()).tools
    schemas = {tool.name: tool.outputSchema for tool in tools}
    assert set(schemas) == {"list_inforesources", "get_inforesource", "get_subgraph", "count_graph"}
    assert all(schemas.values()), "Every tool needs an output schema"
    traces = []

    async def call(name, args, expected_error=None):
        result = await session.call_tool(name, args)
        data = result.structuredContent
        if expected_error == "schema":
            assert result.isError, "Expected invalid argument failure"
        else:
            assert data is not None, "Expected structured output"
            jsonschema.validate(data, schemas[name])
            if result.isError and expected_error is None:
                error = data.get("error") or {}
                raise RuntimeError(f"{name}: {error.get('code', 'TOOL_ERROR')}: {error.get('message', '')} {error.get('hint', '')}")
            assert result.isError == (expected_error is not None), "Unexpected MCP error flag"
            assert data["ok"] == (expected_error is None), data
            if expected_error:
                assert data["error"]["code"] == expected_error, data
        # Only tool data, never headers, environment, login responses or passwords.
        traces.append({"tool": name, "arguments": args, "isError": result.isError,
                       "result": data if data is not None else {"code": "SDK_ARGUMENT_VALIDATION"}})
        return data["data"] if data and data["ok"] else None

    listing = await call("list_inforesources", {"query": "Система единиц", "limit": 5})
    assert listing["resources"], "Configured resource not found"
    path = listing["resources"][0]["path"]
    graph = await call("get_inforesource", {"path": path})
    assert graph["root"]["id"] == "303808806649860"
    assert graph["resource"]["resource_id"] == "4640056526276364782"
    await call("count_graph", {"path": path, "depth": 0})
    await call("get_inforesource", {"path": "missing"}, "RESOURCE_NOT_CONFIGURED")
    await call("get_subgraph", {"path": path, "depth": 99}, "schema")
    await call("get_subgraph", {"path": path, "depth": True}, "schema")
    if mode == "snapshot":
        await call("get_subgraph", {"path": path, "depth": 1}, "SNAPSHOT_SCOPE")
    else:
        subgraph = await call("get_subgraph", {"path": path, "depth": 1})
        assert subgraph["root"]["successors"], "Depth=1 unexpectedly has no children; inspect API response"
    return traces, [tool.model_dump(mode="json") for tool in tools]


async def run(args, env):
    reports = []
    for transport in (["stdio", "http"] if args.transport == "both" else [args.transport]):
        async with connect(transport, args.mode, env) as session:
            traces, schemas = await exercise(session, args.mode)
        reports.append({"transport": transport, "calls": traces})
        print(f"OK: {transport}, {args.mode}, {len(traces)} tool calls")
    report = {"verified_at_utc": datetime.now(timezone.utc).isoformat(), "mode": args.mode,
              "llm_agent": False, "runs": reports}
    directory = BASE / "data"
    directory.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / f"smoke-{args.mode}-{stamp}.json"
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    (BASE / "tool-schemas.json").write_text(json.dumps(schemas, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Report: {destination}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["snapshot", "live"], default="snapshot")
    parser.add_argument("--transport", choices=["stdio", "http", "both"], default="both")
    args = parser.parse_args()
    env = dict(os.environ, PYTHONUTF8="1")
    # Snapshot verification is independent of any credentials on the machine.
    env.pop("IACPAAS_TOKEN", None)
    env.pop("IACPAAS_API_KEY", None)
    if args.mode == "live":
        try:
            headers = api.auth_headers()
        except api.ClientError as exc:
            parser.exit(1, str(exc) + "\n")
        if "X-API-KEY" in headers:
            env["IACPAAS_API_KEY"] = headers["X-API-KEY"]
        else:
            env["IACPAAS_TOKEN"] = headers["Authorization"].removeprefix("Bearer ")
    try:
        asyncio.run(run(args, env))
    except KeyboardInterrupt:
        parser.exit(130, "\nПроверка отменена.\n")
    except Exception as exc:
        # anyio may wrap failures in nested exception groups on transport cleanup.
        def leaves(error):
            nested = getattr(error, "exceptions", None)
            if nested:
                for child in nested:
                    yield from leaves(child)
            else:
                yield error
        errors = list(leaves(exc))
        details = [str(error) for error in errors if isinstance(error, (RuntimeError, AssertionError))]
        explanation = "; ".join(dict.fromkeys(details)) or "Ошибка MCP-соединения. Проверьте запуск сервера, сеть и свободный локальный порт."
        parser.exit(1, "Проверка не завершена: " + explanation + "\nУспешный отчёт не создан.\n")


if __name__ == "__main__":
    main()
