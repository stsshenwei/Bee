## Why

Bee 已有插件市场，但用户无法直接上传、浏览和下载独立技能，也无法从技能库选择技能用于聊天。增加与插件并列的技能库，让技能从上传、阅读、分发到聊天使用形成完整流程。

## What Changes

- 增加「技能库」导航、搜索筛选和卡片列表，保持现有 Bee 插件市场视觉风格。
- 增加技能详情：元数据、Markdown 说明、SKILL.md 原文、附属文件目录、版本与完整 ZIP 下载；信息组织参考 SkillsMP。
- 支持本地技能 ZIP 与单个 SKILL.md 上传，自动解析名称和描述，预览、补充分类/作者/版本后发布；支持版本更新、下架和恢复。
- 增加工作空间技能启用状态与版本固定，聊天输入区支持按次选择已启用技能；服务端加载选定技能说明用于回答，并显示实际加载记录。
- 聊天启用的第一版含义是读取技能说明并使用已有授权工具，不执行上传的脚本、不安装依赖，也不扩大工具权限。
- 复用插件市场的发布身份及底层文件安全能力，技能独立存储；不自动加入插件分发目录或 RAG 语料。
- 首版来源为本地上传；GitHub 导入、外部市场同步、脚本沙箱、评分与付费体系不在本次范围。

## Capabilities

### New Capabilities
- `skill-library-registry`: 独立技能包校验、元数据、不可变版本、发布权限、存储、下载与下架恢复。
- `skill-library-ui`: 卡片目录、详情阅读、文件浏览、上传发布与版本管理。
- `skill-chat-activation`: 工作空间启用、聊天技能选择、请求级隔离、技能加载和可观察使用记录。

### Modified Capabilities

无。当前 openspec/specs 下没有可修改的主规格；本次以新增规格定义能力，并保持已有聊天和插件接口向后兼容。

## Impact

- 前端：Sidebar、新增 /skills 与 /skills/detail 页面、聊天输入区、共享 API/types、现有 globals.css 与 tokens。
- 后端：新增技能 service/repository/bundle 解析模块与 PostgreSQL 表、技能包对象目录、main.py 路由和 schemas.py；扩展 AgentRuntime、RuntimeSkillsManager 与聊天请求传递链。
- API：新增 /skills 系列管理接口及工作空间启用接口；/chat/stream 增加可选 skill_refs，SSE 保留既有事件并增加技能加载元数据。
- 依赖：复用现有 Markdown 渲染与 ZIP 能力；检查并复用安全 YAML 解析器，缺失时显式加入依赖。
- 文档与验证：更新架构、开发、聊天 UI 和运行时设计文档；增加包安全、权限、版本、请求隔离、聊天兼容及浏览器端到端验证。
