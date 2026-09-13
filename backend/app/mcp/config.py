from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class MCPRuntimeConfig:
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str = ""
    tool_timeout_seconds: float = 300.0
    max_output_chars: int = 6000
    max_events: int = 200
    max_upload_bytes: int = 10 * 1024 * 1024
    server_name: str = "bee-rag"
    server_version: str = "0.1.0"

    @classmethod
    def from_env(cls) -> "MCPRuntimeConfig":
        return cls(
            transport=_env("MCP_TRANSPORT", "stdio").lower(),
            host=_env("MCP_HOST", "127.0.0.1"),
            port=_int_env("MCP_PORT", 8765),
            auth_token=_env("MCP_SERVER_AUTH_TOKEN", ""),
            tool_timeout_seconds=_float_env("MCP_TOOL_TIMEOUT_SECONDS", 300.0),
            max_output_chars=_int_env("MCP_MAX_OUTPUT_CHARS", 6000),
            max_events=_int_env("MCP_MAX_EVENTS", 200),
            max_upload_bytes=_int_env("MCP_MAX_UPLOAD_BYTES", 10 * 1024 * 1024),
            server_name=_env("MCP_SERVER_NAME", "bee-rag"),
            server_version=_env("MCP_SERVER_VERSION", "0.1.0"),
        )

    @property
    def is_network_transport(self) -> bool:
        return self.transport in {"http", "streamable-http"}

    def validate(self) -> None:
        if self.transport not in {"stdio", "http", "streamable-http"}:
            raise ValueError("MCP_TRANSPORT must be one of: stdio, http, streamable-http")
        if self.is_network_transport and not self.auth_token:
            raise ValueError("MCP_SERVER_AUTH_TOKEN is required for Streamable HTTP transport")
        if self.tool_timeout_seconds <= 0:
            raise ValueError("MCP_TOOL_TIMEOUT_SECONDS must be greater than zero")
        if self.max_output_chars <= 0:
            raise ValueError("MCP_MAX_OUTPUT_CHARS must be greater than zero")
        if self.max_events <= 0:
            raise ValueError("MCP_MAX_EVENTS must be greater than zero")
        if self.max_upload_bytes <= 0:
            raise ValueError("MCP_MAX_UPLOAD_BYTES must be greater than zero")


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _int_env(name: str, default: int) -> int:
    value = _env(name, "")
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    value = _env(name, "")
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default
