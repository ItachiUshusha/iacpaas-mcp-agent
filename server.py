"""MCP stdio and Streamable HTTP share the same domain service."""
from __future__ import annotations

import argparse
import asyncio
import json
from typing import Annotated, Callable, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field, ValidationError

from domain import (CountReport, Depth, Error, Fault, FolderChange, FundListing, FundSearchReport, GasCompositionReport, Graph, Listing, NodeLimit,
                    ResourcePath, Result, Selection, Service, StartPath, SearchReport, ComparisonReport, RankingReport, ExportNodeReport)
import iacpaas_client as api


def respond(operation: Callable) -> CallToolResult:
    try:
        result = Result(ok=True, data=operation())
    except Fault as exc:
        result = Result(ok=False, error=Error(code=exc.code, message=exc.message, hint=exc.hint))
    except ValidationError:
        result = Result(ok=False, error=Error(code="INVALID_ARGUMENT", message="Некорректные параметры.", hint="Сверьте аргументы с inputSchema инструмента."))
    except Exception:
        # Do not forward raw exception messages, headers, local paths or credentials.
        result = Result(ok=False, error=Error(code="INTERNAL_ERROR", message="Внутренняя ошибка адаптера.", hint="Проверьте конфигурацию и локальные тесты сервера."))
    data = result.model_dump(mode="json")
    return CallToolResult(isError=not result.ok, structuredContent=data,
                          content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False, sort_keys=True))])


def create_server(service: Service, port=8765) -> FastMCP:
    server = FastMCP("IACPaaS учебный сервер", host="127.0.0.1", port=port,
                     stateless_http=True, json_response=True, log_level="WARNING",
                     transport_security=TransportSecuritySettings(
                         enable_dns_rebinding_protection=True,
                         allowed_hosts=["127.0.0.1:*", "localhost:*"],
                         allowed_origins=["http://127.0.0.1:*", "http://localhost:*"]))
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

    @server.tool(annotations=read)
    async def list_inforesources(
        query: Annotated[str, Field(max_length=200, strict=True)] = "",
        limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 20,
    ) -> Annotated[CallToolResult, Result[Listing]]:
        """Search configured resources by title/path. This is a local catalog, not a live listing of the user's fund; access is checked only when reading."""
        return respond(lambda: service.list_resources(query, limit))

    @server.tool(annotations=read)
    async def list_fund_resources(
        parent: StartPath = "",
        query: Annotated[str, Field(max_length=200, strict=True)] = "",
        limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 20,
    ) -> Annotated[CallToolResult, Result[FundListing]]:
        """Search resource titles/paths in the authenticated My Fund, including subfolders, up to 16 folders per call. Empty parent starts at the fund root. Check truncated/skipped_folders. Returned paths can be read or searched immediately."""
        return await asyncio.to_thread(respond, lambda: service.list_fund_resources(parent, query, limit))

    @server.tool(annotations=read)
    async def search_fund_content(
        queries: Annotated[list[Annotated[str, Field(min_length=1, max_length=100, strict=True)]], Field(min_length=1, max_length=4)],
        parent: StartPath = "",
        depth: Annotated[int, Field(ge=1, le=10, strict=True)] = 10,
    ) -> Annotated[CallToolResult, Result[FundSearchReport]]:
        """Search node contents in up to 12 resources in authenticated My Fund. Supply separate terms, e.g. водород, аргон, примес, состав. Searches inside exports even when no resource title matches. Each report shows counts, sample hits, pointers and export hash. Check truncated, skipped_folders and resource errors; use search_inforesource for more hits in a specific resource."""
        return await asyncio.to_thread(respond, lambda: service.search_fund_content(parent, queries, depth))

    @server.tool(annotations=read)
    async def get_inforesource(path: ResourcePath) -> Annotated[CallToolResult, Result[Graph]]:
        """Read metadata and root (depth=0). Use exact path returned by list_inforesources or list_fund_resources. IDs are strings."""
        return await asyncio.to_thread(respond, lambda: service.read(Selection(path=path)))

    @server.tool(annotations=read)
    async def get_subgraph(path: ResourcePath, depth: Depth = 1, start: StartPath = "",
                           max_nodes: NodeLimit = 100) -> Annotated[CallToolResult, Result[Graph]]:
        """Read a bounded universal export from the root with start='', depth 0..3. For a record found in search use read_export_node with its pointer and export_sha256. Never build start from trail labels or JSON Pointers. A nonempty start is only for an explicitly confirmed platform concept path ending in ';'. Ancestors may be included. Snapshot mode has only depth=0."""
        return await asyncio.to_thread(respond, lambda: service.read(Selection(path=path, depth=depth, start=start, max_nodes=max_nodes)))

    @server.tool(annotations=read)
    async def count_graph(path: ResourcePath, depth: Depth = 0, start: StartPath = "",
                          max_nodes: NodeLimit = 100) -> Annotated[CallToolResult, Result[CountReport]]:
        """Count occurrences, defined IDs and links in a bounded export, including ancestors. start must be empty or a confirmed IACPaaS concept path, never an export JSON Pointer. Never a full graph count. Link targets are not resolved; depth=0 normally yields one root and zero edges."""
        return await asyncio.to_thread(respond, lambda: service.count(Selection(path=path, depth=depth, start=start, max_nodes=max_nodes)))

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=False, openWorldHint=True))
    async def create_folder(
        parent: Annotated[str, Field(max_length=512, strict=True)],
        name: Annotated[str, Field(min_length=1, max_length=200, strict=True)],
        description: Annotated[str, Field(max_length=2000, strict=True)] = "",
    ) -> Annotated[CallToolResult, Result[FolderChange]]:
        """Create a folder in the user's IACPaaS fund, live only. parent='' means fund root; name is one component. Requires an explicit user request. Does not create an inforesource or edit catalog.json. Never automatically retry after a connection error."""
        return await asyncio.to_thread(respond, lambda: service.create_folder(parent, name, description))

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                            idempotentHint=False, openWorldHint=True))
    async def delete_folder(path: ResourcePath, confirm_path: ResourcePath) -> Annotated[CallToolResult, Result[FolderChange]]:
        """Move a folder from My Fund to the IACPaaS recycle bin. Live HTTP server with --login --fund-login required. Call only after the user explicitly requests deletion and gives the exact folder path. confirm_path must equal path. Never retry automatically."""
        return await asyncio.to_thread(respond, lambda: service.delete_folder(path, confirm_path))

    @server.tool(annotations=read)
    async def search_inforesource(
        path: ResourcePath,
        query: Annotated[str, Field(min_length=1, max_length=100, strict=True)],
        depth: Annotated[int, Field(ge=0, le=10, strict=True)] = 10,
        limit: Annotated[int, Field(ge=1, le=30, strict=True)] = 20,
    ) -> Annotated[CallToolResult, Result[SearchReport]]:
        """Search inside a resource found in the catalog or My Fund. Query is a literal case-insensitive substring in name/value/meta/link/comment; try one term, formula or stem at a time. Returns fields, trails, pointers and evidence IDs. Depth 0..10; an empty result only applies to this export and term."""
        return await asyncio.to_thread(respond, lambda: service.search(path, query, depth, limit))

    @server.tool(annotations=read)
    async def read_export_node(
        path: ResourcePath,
        pointer: Annotated[str, Field(max_length=2048, strict=True)],
        export_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)],
        depth: Annotated[int, Field(ge=0, le=10, strict=True)] = 10,
        levels: Annotated[int, Field(ge=0, le=6, strict=True)] = 6,
        max_nodes: NodeLimit = 200,
    ) -> Annotated[CallToolResult, Result[ExportNodeReport]]:
        """Read a selected record and its descendants using pointer, export_sha256 and requested_depth from search_inforesource or analyze_gas_composition. No platform start path is needed. The same-depth export is read again; changed hashes fail with EXPORT_CHANGED. Root pointer is ''. levels limits descendant depth, max_nodes limits size; truncated means descendants were cut by levels. Does not resolve links."""
        return await asyncio.to_thread(respond, lambda: service.read_export_node(
            path, pointer, export_sha256, depth, levels, max_nodes))

    @server.tool(annotations=read)
    async def compare_node_children(
        path: ResourcePath,
        left_pointer: Annotated[str, Field(max_length=2048, strict=True)],
        right_pointer: Annotated[str, Field(max_length=2048, strict=True)],
        export_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)],
        depth: Annotated[int, Field(ge=0, le=10, strict=True)] = 10,
    ) -> Annotated[CallToolResult, Result[ComparisonReport]]:
        """Compute common direct-child labels of two distinct lists selected from search_inforesource. For impurities, first find the actual impurity lists under each gas record, never compare the chemical elements themselves. Supply pointers and hash from the SAME export and depth. Changed exports, empty/unresolved lists fail explicitly. Output contains evidence for both sides of every match. This compares labels, not chemical synonyms; results cover only the returned lists."""
        return await asyncio.to_thread(respond, lambda: service.compare_children(path, left_pointer, right_pointer, export_sha256, depth))

    @server.tool(annotations=read)
    async def rank_records_by_percentage(
        path: ResourcePath,
        group_pointer: Annotated[str, Field(max_length=2048, strict=True)],
        composition_path: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(min_length=1, max_length=8)],
        percentage_path: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(min_length=1, max_length=8)],
        percent_basis: Literal["volume", "mass", "mole"],
        export_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)],
        depth: Annotated[int, Field(ge=1, le=10, strict=True)] = 10,
        limit: Annotated[int, Field(ge=1, le=30, strict=True)] = 10,
    ) -> Annotated[CallToolResult, Result[RankingReport]]:
        """Rank live IACPaaS records under one group by an exact percentage. Use the group's pointer and same export hash/depth from search. Supply exact relative node labels from each record to its composition, then from composition to the component's percentage value. Verify one common percentage basis and units in the graph before calling. Returns rows and evidence for records, composition and percentages. Reports skipped rows."""
        return await asyncio.to_thread(respond, lambda: service.rank_percentages(
            path, group_pointer, composition_path, percentage_path, percent_basis, export_sha256, depth, limit))

    @server.tool(annotations=read)
    async def analyze_gas_composition(
        path: ResourcePath,
        base: Annotated[str, Field(min_length=1, max_length=100, strict=True)],
        compare_with: Annotated[str, Field(max_length=100, strict=True)] = "",
        limit: Annotated[int, Field(ge=1, le=30, strict=True)] = 10,
    ) -> Annotated[CallToolResult, Result[GasCompositionReport]]:
        """Read gas grades from База технологических газов. Use base=Ar, H, He, or the exact class name. For shared impurities pass compare_with for the second class. Returns the intersection of components with an upper limit found in at least one grade of each class, with evidence from both classes. Otherwise ranks grades by the lower bound of their main gas volume percentage and returns full recognized compositions. Keeps ≥ and ≤, never treats bounds as exact. Only the monogas classes are compared."""
        return await asyncio.to_thread(respond, lambda: service.gas_composition(path, base, compare_with, limit))

    return server


def main():
    parser = argparse.ArgumentParser(description="IACPaaS MCP server: graph reads and folder operations")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--mode", choices=["live", "snapshot"], default="live")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--login", action="store_true", help="Interactive login before HTTP startup; credentials remain in memory")
    parser.add_argument("--fund-login", action="store_true", help="Also log in to My Fund to enable folder removal")
    parser.add_argument("--schemas", action="store_true", help="Print tool schemas without starting server")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    if args.login and (args.transport != "http" or args.mode != "live" or args.schemas):
        parser.error("--login requires --transport http --mode live")
    if args.fund_login and not args.login:
        parser.error("--fund-login requires --login")
    with httpx.Client(base_url=api.BASE_URL, timeout=api.TIMEOUT, follow_redirects=False) as web_client:
        fund_session = api.FundSession(web_client) if args.fund_login else None
        try:
            if fund_session:
                username, password = api.read_credentials()
                try:
                    headers = api.signin_headers(username, password)
                    fund_session.login(username, password)
                finally:
                    del username, password
                print("Вход в «Мой Фонд» подтверждён.", flush=True)
            else:
                headers = api.auth_headers() if args.login else None
        except api.ClientError as exc:
            parser.exit(1, str(exc) + "\n")
        server = create_server(Service(args.mode, headers, fund_session=fund_session), args.port)
        if args.schemas:
            schemas = asyncio.run(server.list_tools())
            print(json.dumps([t.model_dump(mode="json") for t in sorted(schemas, key=lambda t: t.name)], ensure_ascii=False, indent=2))
        else:
            server.run(transport="stdio" if args.transport == "stdio" else "streamable-http")


if __name__ == "__main__":
    main()
