import asyncio
from dataclasses import dataclass

from app.mcp.config import MCPRuntimeConfig
from app.mcp.runtime import MCPBackendServices
from app.mcp.server import MCPAuthMiddleware
from app.mcp.tools import RAGMCPToolService
from app.models.knowledge_base import KnowledgeBaseScope


@dataclass
class FakeKB:
    id: str = "kb-1"
    workspace_id: str = "workspace-1"
    name: str = "Main KB"
    description: str = ""
    status: str = "active"

    def to_dict(self):
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "indexing_strategy": {},
            "provider_config": {},
            "aggregate": {"document_count": 1},
        }


class FakeKnowledgeBaseService:
    def __init__(self):
        self.created = []
        self.archived = []

    @property
    def default_workspace_id(self):
        return "workspace-1"

    def list(self, workspace_id=None, include_archived=False):
        return [FakeKB()]

    def get(self, knowledge_base_id, allow_archived=True):
        if knowledge_base_id != "kb-1":
            raise KeyError(knowledge_base_id)
        return FakeKB()

    def create(
        self,
        name,
        description="",
        knowledge_base_type="document",
        is_default=False,
        workspace_id=None,
        indexing_strategy=None,
        provider_config=None,
    ):
        self.created.append(
            {
                "name": name,
                "description": description,
                "knowledge_base_type": knowledge_base_type,
                "is_default": is_default,
                "workspace_id": workspace_id,
                "indexing_strategy": indexing_strategy,
                "provider_config": provider_config,
            }
        )
        return FakeKB(id="kb-created", name=name, description=description)

    def archive(self, knowledge_base_id):
        if knowledge_base_id != "kb-1":
            raise KeyError(knowledge_base_id)
        self.archived.append(knowledge_base_id)
        return FakeKB(status="archived")


class FakeDocumentRepository:
    def __init__(self):
        self.document = {
            "id": "doc-1",
            "workspace_id": "workspace-1",
            "knowledge_base_id": "kb-1",
            "name": "manual.md",
            "file_type": "md",
            "storage_path": "manual.md",
            "parse_status": "parsed",
            "metadata_json": {},
        }
        self.chunks = [
            {
                "id": f"chunk-{idx}",
                "doc_id": "doc-1",
                "content": f"content {idx}",
                "chunk_type": "child",
                "metadata_json": {"source": "manual.md"},
            }
            for idx in range(3)
        ]

    def get_document(self, doc_id, scope=None):
        return dict(self.document) if doc_id == "doc-1" else None

    def list_chunks(self, doc_id=None, scope=None):
        if doc_id == "doc-1":
            return [dict(item) for item in self.chunks]
        return []


class FakeRAGService:
    def __init__(self):
        self.document_repository = FakeDocumentRepository()
        self.query_calls = []
        self.upload_calls = []

    def resolve_scope(self, knowledge_base_ids=None, document_ids=None):
        return KnowledgeBaseScope(
            workspace_id="workspace-1",
            selected_knowledge_base_ids=tuple(knowledge_base_ids or ("kb-1",)),
            document_ids=tuple(document_ids or ()),
            compatibility_default=not bool(knowledge_base_ids),
        )

    def list_documents(self, scope=None, filters=None):
        return [
            {
                **self.document_repository.document,
                "source": "manual.md",
                "chunks": 3,
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        ]

    def save_uploaded_document(self, filename, content, relative_path=None, batch_id=None, scope=None):
        self.upload_calls.append(
            {
                "filename": filename,
                "content": content,
                "relative_path": relative_path,
                "batch_id": batch_id,
                "scope": scope,
            }
        )
        return {
            "doc_id": "doc-uploaded",
            "source": relative_path or filename,
            "filename": filename,
            "size": len(content),
            "parse_status": "parsed",
            "chunks": 2,
            "error": None,
        }

    def hybrid_retrieve_hits(self, question, scope=None):
        return [
            {
                "content": "retrieved evidence",
                "score": 0.91,
                "metadata": {"doc_id": "doc-1", "source": "manual.md", "matched_child_ids": ["chunk-1"]},
            }
        ]

    def recall_parent_hits(self, hits, scope=None):
        return hits

    def extract_sources(self, hits):
        return [{"source": hit["metadata"]["source"], "score": hit["score"]} for hit in hits]

    def answer_query(self, question, top_k=None, filters=None, scope=None):
        self.query_calls.append({"question": question, "top_k": top_k, "filters": filters, "scope": scope})
        return {
            "answer": "grounded answer",
            "citations": [{"source": "manual.md"}],
            "used_chunks": ["chunk-1"],
            "confidence": 0.91,
            "debug_info": {"ok": True},
        }

    def stream_answer(self, question, hits=None, conversation_context=None, memory_context=None, scope=None):
        yield "hello"
        yield " world"


class FakeWikiPage:
    def to_dict(self):
        return {
            "slug": "intro",
            "title": "Intro",
            "content_markdown": "# Intro",
            "source_refs": [{"doc_id": "doc-1"}],
        }


class FakeWikiService:
    def search_pages(self, scope, query, limit=10, status="published"):
        return [{"slug": "intro", "title": "Intro", "summary": "summary", "snippet": query}]

    def get_page(self, scope, slug):
        if slug != "intro":
            raise KeyError(slug)
        return FakeWikiPage()


def build_tool_service(max_output_chars=6000):
    rag = FakeRAGService()
    return RAGMCPToolService(
        MCPBackendServices(
            rag_service=rag,
            knowledge_base_service=FakeKnowledgeBaseService(),
            document_repository=rag.document_repository,
            wiki_page_service=FakeWikiService(),
            conversation_service=None,
        ),
        MCPRuntimeConfig(max_output_chars=max_output_chars, max_events=10),
    )


def test_tool_list_is_curated_and_annotated():
    service = build_tool_service()
    names = [tool.name for tool in service.list_tools()]
    assert names == [
        "kb_list",
        "kb_view",
        "kb_create",
        "kb_delete",
        "doc_list",
        "doc_view",
        "doc_upload",
        "chunk_list",
        "search_chunks",
        "rag_query",
        "chat",
        "wiki_search",
        "wiki_read_page",
    ]
    assert "delete_document" not in names
    assert "storage_reset" not in names
    assert "wiki_write_page" not in names
    specs = {tool.name: tool for tool in service.list_tools()}
    assert specs["kb_list"].annotations["readOnlyHint"] is True
    assert specs["kb_create"].input_schema["required"] == ["name", "description"]
    assert specs["kb_delete"].annotations["destructiveHint"] is True


def test_argument_validation_returns_structured_error():
    service = build_tool_service()
    result = service.call_tool("search_chunks", {"query": "", "kb_id": "kb-1"})
    assert result.is_error
    assert result.structured_content["error"]["code"] == "invalid_arguments"


def test_knowledge_document_chunk_and_search_tools():
    service = build_tool_service()
    assert service.call_tool("kb_list", {}).structured_content["items"][0]["id"] == "kb-1"
    assert service.call_tool("kb_view", {"kb_id": "kb-1"}).structured_content["item"]["name"] == "Main KB"
    docs = service.call_tool("doc_list", {"kb_id": "kb-1"}).structured_content
    assert docs["total"] == 1
    assert service.call_tool("doc_view", {"kb_id": "kb-1", "doc_id": "doc-1"}).structured_content["item"]["id"] == "doc-1"
    chunks = service.call_tool("chunk_list", {"kb_id": "kb-1", "doc_id": "doc-1", "limit": 2}).structured_content
    assert len(chunks["chunks"]) == 2
    assert chunks["truncated_at_limit"] is True
    search = service.call_tool("search_chunks", {"kb_id": "kb-1", "query": "manual"}).structured_content
    assert search["results"][0]["content"] == "retrieved evidence"


def test_create_and_delete_knowledge_base_tools():
    service = build_tool_service()
    created = service.call_tool(
        "kb_create",
        {"name": "Policies", "description": "Internal policy documents", "knowledge_base_type": "document"},
    ).structured_content
    assert created["item"]["id"] == "kb-created"
    assert created["item"]["name"] == "Policies"
    kb_service = service.services.knowledge_base_service
    assert kb_service.created[0]["description"] == "Internal policy documents"

    deleted = service.call_tool("kb_delete", {"kb_id": "kb-1"}).structured_content
    assert deleted["status"] == "archived"
    assert deleted["item"]["status"] == "archived"
    assert kb_service.archived == ["kb-1"]


def test_upload_document_tool_indexes_content():
    service = build_tool_service()
    uploaded = service.call_tool(
        "doc_upload",
        {"kb_id": "kb-1", "filename": "note.md", "content": "# Hello", "relative_path": "notes/note.md"},
    ).structured_content
    assert uploaded["item"]["doc_id"] == "doc-uploaded"
    assert uploaded["item"]["parse_status"] == "parsed"
    rag_service = service.services.rag_service
    assert rag_service.upload_calls[0]["filename"] == "note.md"
    assert rag_service.upload_calls[0]["content"] == b"# Hello"
    assert rag_service.upload_calls[0]["scope"].knowledge_base_id == "kb-1"


def test_upload_document_requires_one_content_source():
    service = build_tool_service()
    result = service.call_tool("doc_upload", {"kb_id": "kb-1", "filename": "note.md"})
    assert result.is_error
    assert result.structured_content["error"]["code"] == "invalid_arguments"


def test_rag_query_and_wiki_tools():
    service = build_tool_service()
    rag = service.call_tool("rag_query", {"kb_id": "kb-1", "question": "What is this?"}).structured_content
    assert rag["result"]["answer"] == "grounded answer"
    wiki_results = service.call_tool("wiki_search", {"kb_id": "kb-1", "query": "intro"}).structured_content
    assert wiki_results["items"][0]["slug"] == "intro"
    page = service.call_tool("wiki_read_page", {"kb_id": "kb-1", "slug": "intro"}).structured_content
    assert page["page"]["content_markdown"] == "# Intro"


def test_chat_returns_buffered_projection():
    service = build_tool_service()
    result = service.call_tool("chat", {"kb_id": "kb-1", "message": "hello"}).structured_content
    assert result["answer"] == "hello world"
    assert result["sources"] == [{"source": "manual.md", "score": 0.91}]
    assert result["events"][0]["type"] == "sources"
    assert result["events"][-1]["type"] == "final"
    assert result["assistant_message_id"].startswith("mcp-msg-")


def test_large_output_is_truncated_with_metadata():
    service = build_tool_service(max_output_chars=20)
    result = service.call_tool("rag_query", {"kb_id": "kb-1", "question": "What is this?"})
    assert result.structured_content["_mcp"]["truncated"] is True
    assert "[truncated]" in result.text


def test_network_auth_middleware_rejects_and_accepts():
    calls = []

    async def app(scope, receive, send):
        calls.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def invoke(headers):
        sent = []

        async def send(message):
            sent.append(message)

        middleware = MCPAuthMiddleware(app, "secret")
        await middleware({"type": "http", "headers": headers}, None, send)
        return sent

    rejected = asyncio.run(invoke([]))
    assert rejected[0]["status"] == 401
    accepted = asyncio.run(invoke([(b"authorization", b"Bearer secret")]))
    assert accepted[0]["status"] == 200
    assert len(calls) == 1


def test_network_config_requires_token():
    config = MCPRuntimeConfig(transport="streamable-http", auth_token="")
    try:
        config.validate()
    except ValueError as exc:
        assert "MCP_SERVER_AUTH_TOKEN" in str(exc)
    else:
        raise AssertionError("network transport without token should fail")
