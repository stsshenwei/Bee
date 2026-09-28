# Backend skill-library implementation report

## Files

- `backend/app/services/skills/models.py`: settings, stable domain errors, persistence models, and frozen request-local `ResolvedSkill`.
- `backend/app/services/skills/bundle.py`: bounded UTF-8 Markdown and ZIP validation, safe YAML, deterministic ZIP generation, and bounded file reads.
- `backend/app/services/skills/memory_repository.py`: test repository with immutable version and pinned activation semantics.
- `backend/app/services/skills/postgres_repository.py`: PostgreSQL DDL and repository using `PostgresDatabase`.
- `backend/app/services/skills/service.py`: publication, storage, catalog/detail, withdrawal/restore, activation, and request-local resolution.
- `backend/app/services/skills/router.py`: dependency-injected FastAPI router for the agreed `/skills` and workspace routes.
- `backend/tests/test_skill_service.py`, `backend/tests/test_skill_routes.py`: security, lifecycle, snapshot, ownership, runtime, and route tests.

Skill blobs are stored below the `storage_dir` supplied to `SkillService`; callers should configure a directory separate from `RAG_DATA_DIR`.
Publications use unique immutable blob keys. A failed uncommitted write removes only its own blob; an uncertain transaction outcome retains the blob so a committed row cannot be broken. `cleanup_orphan_blobs(grace_seconds)` provides the repository-aware primitive for a manual maintenance command and defaults to a 24-hour grace period.

## Public interfaces

```python
SkillService(repository, storage_dir, settings=None)
SkillSettings.from_env()
service.resolve(workspace_id, refs, runtime_enabled) -> tuple[ResolvedSkill, ...]
service.workspace_skills(workspace_id, runtime_enabled=True) -> dict
create_skill_router(get_service, get_marketplace, validate_workspace, get_runtime_enabled)
```

`ResolvedSkill` is frozen and exposes `skill_id`, `name`, `version`, `sha256`, `markdown`, `runtime_name`, and `public_metadata()`.

The router reuses the injected marketplace service for bearer-token resolution. Publication requires publish/admin scope, owner changes require owner/admin, and activation requires admin.

## Validation

`backend/.venv-new/Scripts/python.exe -m pytest tests/test_skill_service.py tests/test_skill_routes.py -q`

Result: `18 passed`.
