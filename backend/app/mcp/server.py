from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any

from app.mcp.config import MCPRuntimeConfig
from app.mcp.runtime import load_backend_services
from app.mcp.tools import RAGMCPToolService, ToolSpec

logger = logging.getLogger(__name__)


class MCPAuthMiddleware:
    def __init__(self, app: Any, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        provided = ""
        authorization = headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
        elif headers.get("x-mcp-auth-token"):
            provided = headers["x-mcp-auth-token"].strip()
        if not provided or not secrets.compare_digest(provided, self.token):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        await self.app(scope, receive, send)


def build_tool_service(config: MCPRuntimeConfig | None = None) -> RAGMCPToolService:
    return RAGMCPToolService(load_backend_services(), config or MCPRuntimeConfig.from_env())


def build_mcp_server(tool_service: RAGMCPToolService, config: MCPRuntimeConfig):
    try:
        import mcp.types as types
        from mcp.server import NotificationOptions, Server
        from mcp.server.models import InitializationOptions
    except ImportError as exc:
        raise RuntimeError("The 'mcp' package is required. Install backend/requirements.txt.") from exc

    app = Server(config.server_name)

    @app.list_tools()
    async def list_tools():
        return [_to_mcp_tool(types, spec) for spec in tool_service.list_tools()]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None = None):
        result = await asyncio.to_thread(tool_service.call_tool, name, arguments or {})
        content = [types.TextContent(type="text", text=result.text)]
        call_result = getattr(types, "CallToolResult", None)
        if call_result is not None:
            try:
                return call_result(
                    content=content,
                    structuredContent=result.structured_content,
                    isError=result.is_error,
                )
            except TypeError:
                pass
            try:
                return call_result(
                    content=content,
                    structured_content=result.structured_content,
                    isError=result.is_error,
                )
            except TypeError:
                pass
        return content

    def init_options() -> InitializationOptions:
        return InitializationOptions(
            server_name=config.server_name,
            server_version=config.server_version,
            capabilities=app.get_capabilities(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
        )

    return app, init_options


async def run_stdio(config: MCPRuntimeConfig | None = None) -> None:
    config = config or MCPRuntimeConfig.from_env()
    config = MCPRuntimeConfig(**{**config.__dict__, "transport": "stdio"})
    config.validate()
    try:
        import mcp.server.stdio
    except ImportError as exc:
        raise RuntimeError("The 'mcp' package is required. Install backend/requirements.txt.") from exc
    app, init_options = build_mcp_server(build_tool_service(config), config)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, init_options())


async def run_streamable_http(config: MCPRuntimeConfig | None = None) -> None:
    config = config or MCPRuntimeConfig.from_env()
    if config.transport == "http":
        config = MCPRuntimeConfig(**{**config.__dict__, "transport": "streamable-http"})
    config.validate()
    try:
        import uvicorn
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from starlette.applications import Starlette
        from starlette.routing import Mount
    except ImportError as exc:
        raise RuntimeError("Streamable HTTP MCP transport requires 'mcp', 'starlette', and 'uvicorn'.") from exc

    app, _init_options = build_mcp_server(build_tool_service(config), config)
    session_manager = StreamableHTTPSessionManager(
        app=app,
        event_store=None,
        json_response=False,
        stateless=True,
    )

    @asynccontextmanager
    async def lifespan(_app):
        async with session_manager.run():
            yield

    starlette_app = Starlette(
        routes=[Mount("/mcp", app=session_manager.handle_request)],
        lifespan=lifespan,
    )
    starlette_app = MCPAuthMiddleware(starlette_app, config.auth_token)
    logger.info("Starting MCP Streamable HTTP server on %s:%s at /mcp", config.host, config.port)
    uvicorn_config = uvicorn.Config(starlette_app, host=config.host, port=config.port, log_level="info")
    await uvicorn.Server(uvicorn_config).serve()


async def run(config: MCPRuntimeConfig | None = None) -> None:
    config = config or MCPRuntimeConfig.from_env()
    if config.transport == "stdio":
        await run_stdio(config)
    else:
        await run_streamable_http(config)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Bee RAG MCP server")
    parser.add_argument("--transport", choices=["stdio", "http", "streamable-http"], default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.INFO)
    base = MCPRuntimeConfig.from_env()
    config = MCPRuntimeConfig(
        **{
            **base.__dict__,
            **{key: value for key, value in {"transport": args.transport, "host": args.host, "port": args.port}.items() if value is not None},
        }
    )
    asyncio.run(run(config))


def _to_mcp_tool(types: Any, spec: ToolSpec):
    kwargs = {
        "name": spec.name,
        "description": spec.description,
        "inputSchema": spec.input_schema,
    }
    annotations_type = getattr(types, "ToolAnnotations", None)
    if annotations_type is not None:
        try:
            kwargs["annotations"] = annotations_type(
                title=spec.name,
                destructiveHint=spec.annotations.get("destructiveHint"),
                readOnlyHint=spec.annotations.get("readOnlyHint"),
                idempotentHint=spec.annotations.get("idempotentHint"),
                openWorldHint=spec.annotations.get("openWorldHint"),
            )
        except TypeError:
            try:
                kwargs["annotations"] = annotations_type(
                    title=spec.name,
                    destructive_hint=spec.annotations.get("destructiveHint"),
                    read_only_hint=spec.annotations.get("readOnlyHint"),
                    idempotent_hint=spec.annotations.get("idempotentHint"),
                    open_world_hint=spec.annotations.get("openWorldHint"),
                )
            except TypeError:
                pass
    return types.Tool(**kwargs)
