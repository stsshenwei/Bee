## Context

现有 `/plugins` 与 `/plugins/detail` 已实现插件卡片、ZIP 预检、发布和版本下载；`services/marketplace/` 提供身份、PostgreSQL 元数据和文件存储。插件校验要求 plugin.json，无法直接作为独立技能上传入口。

`RuntimeSkillsManager` 当前读取预置目录，`AgentRuntime` 在实例上持有 manager，并向提示词注入技能目录；`read_skill` 可读取说明，`execute_skill` 因缺少安全沙箱返回不可用。新增动态技能必须采用请求级快照，不能修改共享 manager 导致并发会话串用。

页面沿用根 DESIGN.md 的白色阅读面、森林绿操作按钮、紧凑卡片和系统字体。参考 SkillsMP 的列表与详情信息结构（https://skillsmp.com/search、https://skillsmp.com/creators/anthropics/skills/skills-frontend-design），不复制第三方统计数据或自动抓取内容。

## Goals / Non-Goals

**Goals:** 本地上传、技能卡片与详情、完整下载、不可变版本、下架恢复、工作空间启用以及聊天显式选择和加载记录。

**Non-Goals:** GitHub 导入、市场同步、自动执行脚本、依赖安装、自动扩展 MCP/工具权限、评分付费、全文向量检索。浏览器文件预览与聊天读取附属文件分开：首版聊天只加载 SKILL.md，附属资源供浏览和下载。

## Decisions

### 1. 独立技能域，共用基础设施

新增 `services/skills/`，包括 bundle parser、service、repository、runtime resolver；路由保持薄层。复用市场 token 校验与 ZIP 路径/大小约束，不把技能强制包装成插件，也不将其写入插件 marketplace.json 或 RAG 语料。技能名在发布者下唯一，客户端始终以 skill_id 标识。

数据模型：
- `skill_package`: id、owner_id、name、description、category、tags、author、source_url、license、created_at、updated_at。
- `skill_version`: id、skill_id、version、status(published/yanked)、manifest_json、skill_markdown、file_index_json、blob_key、sha256、size、created_at；唯一约束 (skill_id, version)，版本内容与元数据不可变。
- `workspace_skill_activation`: workspace_id、skill_id、version_id、enabled、updated_at；工作空间与技能联合唯一。启用权限绑定服务端解析的工作空间，不信任任意客户端 ID。

第一版发布内容公开可读，写入复用 publish/admin token 与 owner 校验。工作空间启用修改首版仅允许现有 admin token；普通聊天可选择该工作空间已启用技能。未来用户角色模型可替换该管理权限，不依赖尚未实现的账号系统。

### 2. 上传一个技能，下载完整目录

接受 UTF-8 SKILL.md 或 ZIP。ZIP 允许根 SKILL.md，或剥离唯一公共顶层目录后根 SKILL.md；多个技能根时返回明确错误。使用安全 YAML 解析 frontmatter，name 和 description 必填；缺失 author/category/tags/license/version 由表单补充，首次 version 默认 1.0.0。发布身份 owner 与声明作者 author 分开。

预检不发布；提交时重新校验实际字节和元数据。保留脚本及资源但不运行。拒绝路径穿越、绝对路径、符号链接、规范化后重复路径和超限压缩包；限制上传大小、解压总量、文件数、单文件大小和 Markdown 大小。Markdown 预览不启用原始 HTML，拒绝危险链接协议；非文本只展示文件信息和下载。

统一生成规范 ZIP（根目录包含 SKILL.md 与原有相对资源），记录哈希；单文件上传也产生 ZIP。原始 SKILL.md 正文不因补充注册元数据而重写，详情展示区分包字段与原文。临时对象写入、最终对象原子移动、数据库事务发布；失败清理临时对象，崩溃遗留由有宽限期的孤儿清理处理，目录不暴露半成品。

### 3. 不可变版本与显式更新

重复版本返回 409，不覆盖。新版发布不自动升级已启用版本；管理者显式切换。下架版本移出目录、禁止新下载和新请求使用；详情管理视图保留状态并允许恢复。已开始请求使用其快照完成，下一请求重新校验。首版仅软下架，不提供物理清除；若全部版本下架，技能从普通列表消失。

### 4. 页面和 API

`/skills`：标题和上传按钮、搜索/分类/作者筛选与更新时间排序、分页卡片；卡片显示名称、简介、作者、分类、最新版本和更新时间。`/skills/detail?id=...`：返回导航、标题、下载、启用/停用；主体为说明/文件/版本，侧栏为元数据与工作空间启用状态。上传采用弹窗：选文件→解析预览→补充信息→发布。保留列表筛选和返回位置。移动端单列，键盘可完成所有操作。

拟定 API：
- `GET /skills`：q/category/author/sort/cursor/limit，返回 items/next_cursor。
- `POST /skills/validate`：multipart 文件，返回 parsed_metadata/file_index/errors；受上传权限和限制约束。
- `POST /skills`：首个版本；`POST /skills/{id}/versions`：发布新版。
- `GET /skills/{id}`、`GET /skills/{id}/versions/{version}`：详情和固定版本。
- `GET /skills/{id}/versions/{version}/files?path=...`：有界文本预览；`GET .../download`：完整 ZIP。
- `PATCH /skills/{id}/versions/{version}`：published/yanked 状态切换，仅 owner/admin。
- `GET /workspaces/{workspace_id}/skills` 与 `PUT /workspaces/{workspace_id}/skills/{skill_id}`：查询启用项、设置 enabled/version；变更需 admin 并校验工作空间。
- `ChatRequest.skill_refs?: [{skill_id, version}]`：显式聊天技能选择；省略或空数组不附加库技能，保持原预置技能行为。

业务错误返回稳定 code/message；未授权 401、越权 403、不存在 404、版本冲突 409、格式不合法 422、大小超限 413。聊天技能失效在启动模型调用前返回可操作错误，不静默忽略或升级。

### 5. 工作空间启用与按次选择

详情页启用只允许该工作空间使用指定版本，不代表每个聊天都加载。聊天输入区“技能”多选菜单展示可用启用项和版本，选中项显示可移除标签；选择只在当前打开会话中保留，新建会话清空。每次请求传固定版本引用，后端从会话/KB 范围解析权威 workspace 并逐项核验。

所有四种聊天模式支持显式选中技能：统一 runtime 路径在生成前加载正文，即使 quick 模式没有工具调用也能应用。未进入 runtime 的旧路径明确返回 skill_runtime_unavailable，不能悄悄丢弃选择。服务器 `AGENT_RUNTIME_SKILLS_ENABLED` 和工作空间插件 skills 策略仍是上限；不可用时 UI 解释原因。

创建不可变请求级 resolved skills，包含 id、owner/name、version、hash、正文；传入消息构建与工具上下文，不更新全局 manager。显式选择的正文一次性加载，`read_skill` 仅允许当前请求可见条目；库技能使用带 ID 的唯一引用避免同名冲突，预置技能保持旧名称调用。

新增配置限制所选数量和正文总预算。超预算拒绝并提示减少选择，不静默截断。技能正文作为用户选定的指导资料，不能覆盖系统规则、知识范围和工具授权；上传 scripts 仍不可执行。仅依赖脚本或附属资源的技能不保证可完整运行，界面明确说明“聊天读取技能说明”。

### 6. 可观察性和兼容性

添加 `skills_loaded` SSE 元数据事件（id、name、version、hash），聊天显示“已加载技能”而不声称模型已成功执行。将相同快照元数据持久化到助手消息元数据，并纳入回放缓存；旧消息缺字段仍正常渲染。保留 sources/token/agent events/[DONE] 的含义、取消与续传机制。只记录公开加载信息，不展示隐含推理或发布 token。

## Risks / Trade-offs

- 上传指令可能试图扩展权限 → 运行时授权独立执行，正文不得修改工具注册表；首版无脚本执行。
- 共享 runtime 造成技能串用 → 请求级 immutable resolver，增加双工作空间并发测试。
- 元数据已写但文件不可用 → 暂存、原子移动、事务提交、失败清理及对象完整性检查。
- 附属文件未自动进入聊天 → 明确展示能力边界，支持完整下载，后续另立受限资源读取提案。
- 现有身份体系缺少普通用户角色 → 写入沿用 token，工作空间启用先限 admin；本次不临时发明账号体系。
- 技能内容过长或多个技能相互冲突 → 有界显式选择，显示实际版本，预算超限明确报错；不保证互相矛盾的指令同时满足。

## Migration Plan

1. 增量创建三个技能表和独立对象目录，不重建现有业务表、插件包或向量索引。
2. 部署 API 与解析、下载测试，默认没有库技能启用，不自动导入本地 .codex/.agents 技能。
3. 部署列表、详情与上传 UI，再接入工作空间设置和按次聊天解析。
4. 使用测试技能验证上传→查看→下载→启用→聊天加载→重放→停用；回归无技能聊天及插件市场。
5. 回滚前端入口和新请求字段，再回滚 API/runtime；保留新增表和包以便恢复，已保存消息对未知元数据向后兼容。

## Open Questions

无阻塞项。当前按用户确认涵盖管理、下载和 Bee 聊天启用；本地上传为首版默认来源。GitHub 导入及脚本执行需独立后续提案。
