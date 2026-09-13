from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class MCPBackendServices:
    rag_service: Any
    knowledge_base_service: Any
    document_repository: Any
    wiki_page_service: Any | None = None
    conversation_service: Any | None = None


def load_backend_services() -> MCPBackendServices:
    from app import main

    rag_service = main.rag_service
    return MCPBackendServices(
        rag_service=rag_service,
        knowledge_base_service=getattr(rag_service, "knowledge_base_service", None),
        document_repository=getattr(rag_service, "document_repository", None),
        wiki_page_service=getattr(rag_service, "wiki_page_service", None),
        conversation_service=getattr(main, "conversation_service", None),
    )
