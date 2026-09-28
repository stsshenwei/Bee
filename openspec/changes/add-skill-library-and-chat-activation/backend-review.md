# Backend skill review

Scope: `backend/app/services/skills` and `backend/tests/test_skill*`, against skill library registry and chat activation specifications. Re-review completed after implementation fixes. No outstanding high/medium issue from the four reproduced findings below.

## Final verification

- Re-ran `backend/.venv-new/Scripts/python.exe -m pytest tests/test_skill_service.py tests/test_skill_routes.py -q` from `backend`: **18 passed**.
- Finding 1 fixed in `bundle.py:35`: supported metadata is projected and typed before serialization; arbitrary YAML values cannot enter persisted metadata.
- Finding 2 fixed in `service.py:71`: a committed row referencing the current blob is preserved; uncertain outcomes retain the blob for recovery. Regression exercises a committed-then-raised repository.
- Finding 3 fixed in `bundle.py:77`: standalone uploads enforce member and total decompression limits, with a parity regression.
- Finding 4 fixed in `service.py:122`: version existence is checked before mutation, with an existing-package/missing-version regression.
- ZIP member read failures now map to typed validation errors. No live PostgreSQL execution was performed; the integration-test gap below remains a limitation, not a demonstrated defect.

## Original findings (resolved)

1. **High — unvalidated YAML metadata can break the public catalog.** `service.py:_merge_metadata` validates supplied form metadata but trusts parsed YAML. Publish a valid version, then version `2.0.0` with `tags: 3` in YAML: publication succeeds, and every catalog request fails in `_package_view` with `TypeError: 'int' object is not iterable`. YAML `created: 2026-09-21` also passes validation but cannot be serialized by the PostgreSQL repository. Validate supported parsed fields and normalize/reject unsupported non-JSON types and recursive aliases before publication. Include regression coverage for malformed later versions and timestamp/alias metadata.

2. **High — uncertain commit outcome can delete a committed version's bundle.** `service.py:publish` unlinks the blob immediately when `add_version` raises, before examining persisted state. Reproduced with a repository that commits via `super().add_version()` then raises `OSError('commit acknowledgement lost')`: publication reports a conflict, the published version remains visible, and download raises `FileNotFoundError`. Check whether the stored row references this attempt's blob before deletion; preserve blobs for orphan recovery if the outcome cannot be determined. Test committed-then-raised and definitely-rolled-back outcomes separately.

3. **Medium — standalone Markdown bypasses configured member/decompressed limits.** `bundle.py:parse_skill_bundle` enforces `max_file_bytes` and `max_uncompressed_bytes` only in the ZIP branch. A valid 50-byte Markdown upload validates with both limits set to 1. Apply relevant limits to every upload representation and test Markdown and ZIP parity.

4. **Medium — withdrawing/restoring a nonexistent version returns an internal error.** `service.py:set_status` checks the package and owner but not the version. For an owned existing package, changing `99.0.0` raises `KeyError` in memory and passes `None` into `_version_view` in PostgreSQL. Return the existing typed 404 by resolving the record before mutation, with service and route regression coverage.

## Validation and gaps

- Baseline command: `backend/.venv-new/Scripts/python.exe -m pytest tests/test_skill_service.py tests/test_skill_routes.py -q` from `backend`: **9 passed**.
- Executed standalone counterexamples for all four findings; they reproduce despite the passing baseline suite.
- PostgreSQL schema/query review found parameterized writes and unique `(skill_id, version)` enforcement. No live PostgreSQL integration test was run; current focused tests exercise only memory storage, so transaction rollback, concurrent publication, activation SQL and restart persistence still lack execution evidence.
- Owner checks, immutable existing-version rejection, bounded in-bundle preview and pinned resolution were inspected. No additional concrete authorization bypass was established within this scope.
