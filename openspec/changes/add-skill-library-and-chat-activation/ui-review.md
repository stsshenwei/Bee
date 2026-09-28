# UI review

## 1. Disposition

**FIX.** The catalog, reading surface, forms, and picker follow the approved Bee direction; no redesign is needed. Resolve the concrete interaction/accessibility findings below, then perform a scoped review.

Follow-up disposition on 2026-09-22: the concrete findings below were addressed in the implementation. Mobile navigation now uses a compact icon-over-label layout with unbroken labels; enabled yanked versions can be disabled; the detail return action uses a catalog return target with `/skills` fallback; missing `id` renders an error state; tabs implement roving focus with Arrow/Home/End; the picker restores focus to its trigger on Escape; activation copy uses "切换到" for non-current versions; token guidance is visible near the disabled activation action. The smoke script now captures the validated mobile upload form state.

## 2. Reviewed scope and evidence

Opened all ten screenshots in `frontend/.artifacts/skills/`: catalog, detail, and upload at desktop, 390px, and 320px, plus `chat-skills-320.png`. Read root `DESIGN.md`, the change design, craft floor, all skill page components, `SkillPicker.tsx`, `ModalSurface.tsx`, skill API helpers, and relevant CSS. The screenshots use clearly identified test content. Existing browser overflow checks are supplied evidence, not independently rerun here.

The white surfaces, forest actions, compact cards, system typography, and restrained borders match the incumbent direction. Desktop hierarchy is clear. Catalog and detail content remain readable at 320px. Modal code supplies initial focus, focus containment, Escape dismissal, background inertness, scroll locking, and focus restoration. Inputs have accessible labels, validation errors use alerts, and visible focus styles exist.

## 3. Material findings

1. **Mobile navigation wraps labels into vertical character columns.** `catalog-320.png`, `detail-320.png`, `upload-320.png`, and `chat-skills-320.png` show every navigation label split into single characters; at 390px, 知识库 and 技能库 still split. The fourth navigation entry exceeds the current horizontal allocation. In `frontend/app/globals.css`, adjust the small-screen `.sidebar-nav` button layout/gaps/padding or use icons above unbroken labels. Preserve readable full labels at both widths.

2. **An enabled, subsequently yanked version cannot be disabled from its detail view.** `frontend/app/skills/detail/page.tsx` disables the activation button whenever `version.status !== "published"`, including when `activeVersion` is true and the action is 停用技能. Permit disabling the currently enabled version regardless of publication status; only block new activation of yanked versions. This is a code-proven state gap not exercised by the supplied screenshots.

3. **The return control does not reliably return to the catalog.** `frontend/app/skills/detail/page.tsx` uses `history.length > 1` to invoke `router.back()`. A detail opened from a chat loaded-skill link therefore returns to chat despite its 返回技能库 label; an externally opened detail may leave Bee. Preserve an explicit catalog return URL including filters/cursor, and fall back to `/skills` when no catalog origin exists.

4. **Missing detail ID leaves a permanent loading state.** In `frontend/app/skills/detail/page.tsx`, `/skills/detail` without an `id` makes the fetch effect return while `loading` stays true. Render a missing-skill state with catalog recovery once URL initialization finishes.

5. **ARIA tabs lack the tab keyboard interaction.** The `role="tablist"` in `frontend/app/skills/detail/page.tsx` exposes all tabs as ordinary tab stops and provides no arrow/Home/End navigation. Implement roving `tabIndex` and horizontal keyboard movement/activation, or use ordinary buttons without tab semantics. Verify keyboard access to the displayed panel as well.

## 4. Minor findings

- `SkillPicker.tsx` closes on Escape without restoring focus to its trigger. When Escape is pressed from a checkbox or removal button, the focused element is unmounted. Keep a trigger ref and focus it on keyboard dismissal.
- The detail activation button is disabled without a token, but the explanation is hidden behind 管理权限. Show a short adjacent instruction pointing to 管理权限 and the required administrator token when this is why activation is disabled.
- The activation action says 升级到 for any different version, including an older version. Use 切换到 v… unless semantic version comparison establishes an upgrade.

## 5. Limits and scoped recheck

The mobile upload screenshots show only the initial file-selection state; they do not establish the layout of the longer validated metadata form. Recapture that state at 320px after fixes, including its scrollable footer. Supplied detail screenshots cover the explanation tab, not file/version management, and include only one short skill. No live screen-reader, keyboard, network failure, or actual runtime execution was performed in this review. Recheck corrected mobile navigation, tab/picker keyboard behavior, missing-ID and catalog-return routes, and disabling an enabled yanked version. Backend enforcement and request concurrency are outside this UI review.
