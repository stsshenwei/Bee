# Bee

一个面向企业知识库问答的全栈 RAG 系统，附带一个自托管的**插件市场**。Next.js 构建前端交互，FastAPI 承载文档处理、检索、智能推理、流式回答、知识库管理、插件分发与评测能力。

## 核心能力

- **证据优先的回答**：回答必须来自检索到的知识库证据；证据不足时明确说明无法确定。
- **混合检索**：pgvector 向量检索 + PostgreSQL 关键词召回，RRF 融合去重，可选重排与父块召回；对型号/端口/参数类问题做同源约束过滤。
- **智能推理模式**：LLM 生成同义词/别名锚定关键词后语义扩展，阅读全文证据再生成答案，对外只暴露可审计摘要。
- **多知识库隔离**：workspace 与 knowledge base 范围选择，分阶段上传（批次确认后才解析/切片/嵌入），处理全程可追踪。
- **Agent Runtime 与内置插件目录**：知识检索、Wiki、网页搜索/抓取、数据分析、数据库查询等能力开关，按 workspace 配置。
- **插件市场**：自托管插件注册中心——上传/下载/删除插件包，对外输出 CodeBuddy 兼容的 `marketplace.json`、整体 ZIP 快照与 Git 镜像，供 WorkBuddy/CodeBuddy 等 vibecoding 客户端作为套件源接入。
- **MCP 服务器**：内置 MCP server，把知识库检索、文档上传等能力暴露给外部 AI 客户端（stdio / Streamable HTTP，支持 Bearer 鉴权）。
- **可观测与可评测**：请求级日志 + `X-Trace-ID`、可选 Langfuse、RAG/GraphRAG/Agentic Retrieval 离线评测。

## 技术栈

- Frontend: Next.js App Router
- Backend: FastAPI
- 元数据与检索存储: PostgreSQL（元数据 + pgvector 向量 + 关键词检索）
- 异步处理（可选）: Redis / Celery；默认使用 PostgreSQL 任务行 + 进程内 worker
- 图谱（可选）: Neo4j
- 可观测（可选）: Langfuse
- LLM / embedding: OpenAI 兼容 API

> 旧版 SQLite 元数据与 Milvus 向量存储已退役；相关环境变量（`METADATA_DB_PATH`、`MILVUS_*` 等）不再生效。

## 项目结构

```text
Bee/
  frontend/                    Next.js 前端
    app/
      page.tsx                 对话与知识库主界面
      knowledge/               知识库管理页
      plugins/                 插件市场管理台
      components/              布局与通用组件
      lib/                     API 客户端与类型
  backend/                     FastAPI 后端
    app/
      main.py                  API 入口与服务装配
      services/                检索、知识库、Agent、插件、插件市场、Wiki、评测等
        marketplace/           插件市场域（上传校验/版本/快照/Git 镜像）
        mcp/                   内置 MCP 服务器
      scripts/                 存储重建等维护脚本
    config/prompt_templates/   Prompt 模板
    data/                      知识文件、上传与反馈内容
    evalsets/                  评测集
    tests/                     pytest 测试
  docs/                        架构、开发、API 与设计文档
  openspec/                    变更提案与规格
```

## 快速启动

### 1. 启动后端

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

在 `backend/.env` 中至少配置：

```env
OPENAI_API_KEY=your-api-key
DATABASE_URL=postgresql://user:password@localhost:5432/bee
```

启动服务：

```powershell
uvicorn app.main:app --reload --port 8000
```

健康检查：

```powershell
curl http://localhost:8000/health
```

完整环境变量清单（检索、解析、异步运行时、MCP、插件市场等）见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。

### 2. 启动前端

```powershell
cd frontend
npm install
Copy-Item .env.local.example .env.local
npm run dev
```

浏览器打开 `http://localhost:3000`：侧边栏提供 **对话**（`/chat`）、**知识库**（`/knowledge`）、**插件**（`/plugins`）三个工作区。

### 3. 知识入库

在知识库页面走分阶段上传流程，或对 `backend/data/` 中的文件手动入库：

```powershell
curl -X POST http://localhost:8000/ingest
```

## 插件市场

面向 vibecoding 客户端（WorkBuddy/CodeBuddy 套件源）的插件注册中心：

- **发布**：上传插件 ZIP（含 `.codebuddy-plugin/plugin.json`，可携带 skills/commands/agents/hooks/MCP 配置），服务端做安全校验；版本不可变，支持 yank/恢复/物理删除。
- **分发**：`marketplace.json` 目录、整体 ZIP 快照、Git 裸仓库镜像（`/marketplace/git`）三种产物；私有包按 owner 隔离。
- **管理台**：`/plugins` 页面完成校验预览 → 发布 → 版本管理 → 可见性切换。

套件源可填写（按顺序尝试）：

```text
http://localhost:8000/marketplace/snapshot.zip
http://localhost:8000/marketplace/git
http://localhost:8000/marketplace/marketplace.json
```

服务端需配置 `MARKETPLACE_ADMIN_TOKEN`（或 `MARKETPLACE_OWNER_TOKENS="token=owner:publish"`）。数据模型、API 与失败语义见 [docs/MARKETPLACE.md](docs/MARKETPLACE.md)。

## MCP 服务器

把 Bee 的知识库能力暴露给外部 AI 客户端：

```powershell
cd backend
python -m app.mcp --transport stdio

# 或带鉴权的 HTTP
$env:MCP_SERVER_AUTH_TOKEN="replace-with-a-strong-secret"
python -m app.mcp --transport streamable-http --host 127.0.0.1 --port 8765
```

工具清单与客户端示例见 [docs/MCP.md](docs/MCP.md)。

## 测试

```powershell
# 后端
cd backend
python -m pytest tests/ -q

# 前端
cd frontend
node --test app/lib/api.test.mjs app/lib/marketplace-api.test.mjs app/lib/responsive-css.test.mjs
npm run build
```

## 文档

- [架构说明](docs/ARCHITECTURE.md) — 系统分层与关键机制
- [开发指南](docs/DEVELOPMENT.md) — 环境搭建、环境变量全集、验证命令
- [API 文档](docs/API.md) — 接口契约
- [插件市场](docs/MARKETPLACE.md) — 数据模型、分发契约、令牌配置
- [内置插件](docs/PLUGINS.md) — Agent Runtime 能力目录
- [MCP 服务器](docs/MCP.md) — 工具清单与接入示例
- [后端 RAG Pipeline 设计](docs/design-docs/backend-rag-pipeline.md)
- [前端交互设计](docs/design-docs/frontend-chat-ui.md)
- [openspec/](openspec/) — 变更提案与能力规格

## 存储与维护

活跃存储为 PostgreSQL（元数据、pgvector 向量、关键词索引、任务、会话、评测、插件市场元数据），Neo4j 为可选图谱存储。需要重建存储结构时使用维护脚本：

```powershell
cd backend
python -m app.scripts.rebuild_knowledge_storage --delete-managed-sources --include-neo4j   # 只查看计划
```

执行重建（含备份确认流程、Milvus/Neo4j 原生备份要求）见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。
