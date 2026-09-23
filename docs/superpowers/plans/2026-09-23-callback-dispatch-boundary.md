# Callback Dispatch Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move callback orchestration out of `bridge.callbacks` so panel lifecycle primitives no longer depend on concrete callback-route handlers.

**Architecture:** `bridge.callbacks` remains the low-level owner of panel close/binding helpers. A new `bridge.callback_dispatch` module owns `process_callback()` and depends on both lifecycle primitives and concrete route handlers; `bridge.worker_orchestration` calls that canonical dispatcher. This removes `callbacks -> panel_callback_routes` without adding a back-edge to the dispatcher.

**Tech Stack:** Python 3.11+, unittest/pytest, pytest-xdist, stdlib `ast`, SQLite test fixtures.

**Spec:** `docs/superpowers/specs/2026-09-23-import-cycle-retirement-design.md`

## Global Constraints

- Preserve Telegram callback behavior, persistence semantics, and service composition.
- Do not add a compatibility facade, service locator, dynamic lookup bridge, or import-time mutation.
- Retire `callbacks -> panel_callback_routes`; do not replace it with another cyclic edge.
- Keep the PR independently testable and mergeable from base `4c187043d5fa9275b8410ce9fc00e194da5405a9`.
- Record largest-SCC and total-cyclic-module metrics before and after the change.
- Do not decompose `route_update()` in this PR.

## Review Focus

- Queued callbacks must suppress duplicate callback-query acknowledgement while still routing normally.
- Expired/unbound panel callbacks must still send visible feedback, purge the binding, and close the panel.
- A callback from a different actor must still be rejected before route handlers run.
- The injected `SyncService` must still reach `handle_primary_panel_callback()` unchanged.
- Importing `bridge.callbacks` alone must not import `bridge.panel_callback_routes` as a side effect.

---

### Task 1: Add the RED import-boundary contract

**Files:**
- Create: `tests/test_callback_dispatch_boundary.py`

**Interfaces:**
- Consumes: current `bridge.callbacks` and `bridge.panel_callback_routes` modules.
- Produces: a permanent guard that makes `bridge.callback_dispatch.process_callback` canonical and forbids `callbacks -> panel_callback_routes`.

- [ ] **Step 1: Write the failing architecture tests**

```python
from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys
import unittest

BRIDGE_DIR = Path(__file__).parents[1] / "bridge"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules
```

```python
class CallbackDispatchBoundaryTests(unittest.TestCase):
    def test_callbacks_does_not_import_panel_callback_routes(self):
        self.assertNotIn(
            "bridge.panel_callback_routes",
            imported_modules(BRIDGE_DIR / "callbacks.py"),
        )

    def test_dispatcher_is_canonical_owner(self):
        import bridge.callback_dispatch as callback_dispatch
        import bridge.callbacks as callbacks

        self.assertTrue(callable(callback_dispatch.process_callback))
        for name in (
            "process_callback",
            "ensure_session",
            "DEFAULT_MODEL",
            "answer_callback",
            "handle_group_panel_callback",
            "panel_owner_for_message",
            "panel_session_for_message",
            "send_text",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(callbacks, name))

    def test_callbacks_import_does_not_load_route_module(self):
        code = (
            "import sys\n"
            "import bridge.callbacks\n"
            "assert 'bridge.panel_callback_routes' not in sys.modules\n"
        )
        subprocess.run([sys.executable, "-c", code], check=True)
```

- [ ] **Step 2: Run the RED test**

Run: `pytest -q tests/test_callback_dispatch_boundary.py`

Expected: FAIL because `bridge.callback_dispatch` does not exist and `bridge.callbacks` still imports `bridge.panel_callback_routes`.

- [ ] **Step 3: Do not weaken the test**

The failing assertions define the architecture target. Do not add aliases to `callbacks.py` to make old imports pass.

### Task 2: Move callback orchestration to its canonical owner

**Files:**
- Create: `bridge/callback_dispatch.py`
- Modify: `bridge/callbacks.py`
- Modify: `bridge/worker_orchestration.py`

**Interfaces:**
- Consumes: `BridgeServices`, `RequestContext`, callback lifecycle helpers from `bridge.callbacks`, route handlers from `bridge.panel_callback_routes`, group/help/media/Telegram helpers already used by `process_callback`.
- Produces: `process_callback(db, token, callback, operation_id=None, *, actor_id="", services: BridgeServices) -> None` from `bridge.callback_dispatch`.

- [ ] **Step 1: Create `bridge/callback_dispatch.py` with explicit ordinary imports**

```python
"""Route Telegram callbacks through explicit application dependencies."""
from __future__ import annotations

import sqlite3

from bridge.callback_tokens import resolve_dynamic_callback_token
from bridge.callbacks import close_panel_message, discard_panel_binding, is_session_scoped_panel_callback
from bridge.catalog import answer_callback
from bridge.common import parse_topic_scope
from bridge.composition import BridgeServices, RequestContext
from bridge.config import DEFAULT_MODEL
from bridge.database import panel_owner_for_message, panel_session_for_message
from bridge.groups import handle_group_panel_callback
from bridge.help import handle_enum_callback
from bridge.media import remove_inline_keyboard
from bridge.panel_callback_routes import (
    handle_entity_panel_callback,
    handle_primary_panel_callback,
    handle_provider_model_callback,
)
from bridge.telegram import ensure_session, load_session, send_text
```

- [ ] **Step 2: Move `process_callback()` with behavior preserved**

Use this signature and setup in the new module; copy the existing route calls and argument order exactly:

```python
def process_callback(
    db: sqlite3.Connection,
    token: str,
    callback: dict,
    operation_id: int | None = None,
    *,
    actor_id: str = "",
    services: BridgeServices,
) -> None:
    sender = str(actor_id or (callback.get("from") or {}).get("id", ""))
    message = callback.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    data = str(callback.get("data") or "")
    if not chat_id:
        return

    callback_answer = answer_callback
    if callback.get("_queued"):
        def callback_answer(*_args, **_kwargs):
            return None

    message_id = message.get("message_id")
    bound_session_id = (
        panel_session_for_message(db, chat_id, message_id)
        if message_id else None
    )
    bound_owner_id = (
        panel_owner_for_message(db, chat_id, message_id)
        if message_id else ""
    )
```

```python
    if message_id and bound_owner_id and sender != bound_owner_id:
        feedback = "This panel belongs to another user"
        if callback.get("_queued"):
            send_text(token, chat_id, feedback)
        else:
            callback_answer(token, str(callback.get("id", "")), feedback)
        return

    if message_id and is_session_scoped_panel_callback(data) and not bound_session_id:
        feedback = "Panel expired; reopen it"
        discard_panel_binding(db, chat_id, message_id)
        if callback.get("_queued"):
            send_text(token, chat_id, feedback)
        else:
            callback_answer(token, str(callback.get("id", "")), feedback)
        close_panel_message(db, token, chat_id, callback)
        return

    session = (
        load_session(db, chat_id, bound_session_id, DEFAULT_MODEL)
        if bound_session_id
        else ensure_session(db, chat_id, DEFAULT_MODEL)
    )
    session_id = session["session_id"]
    request_context = RequestContext(db, session_id, sender)
    memory_service = services.memory
    persona_service = services.persona
    sync_service = services.sync
```

```python
    if handle_primary_panel_callback(
        db, token, callback, callback_answer, data, chat_id, message,
        session, session_id, operation_id,
        memory_service=memory_service,
        persona_service=persona_service,
        sync_service=sync_service,
        request_context=request_context,
    ):
        return
    if handle_entity_panel_callback(
        db, token, callback, callback_answer, data, chat_id, message,
        session, session_id, operation_id,
        memory_service=memory_service,
        persona_service=persona_service,
        request_context=request_context,
    ):
        return
    if data.startswith("enum:"):
        handle_enum_callback(
            db, token, chat_id, session, data, message,
            request_context=request_context,
        )
        return
    if data.startswith("group:") or data.startswith("groupchars:") or data.startswith("groupmode:"):
        if parse_topic_scope(chat_id)[1] is None:
            callback_answer(token, str(callback.get("id", "")), "Forum Topic required")
            remove_inline_keyboard(db, token, callback)
            return
```

```python
        if data.startswith("groupchars:") and not data.startswith("groupchars:page:"):
            parts = data.split(":", 2)
            if len(parts) == 3:
                resolved = resolve_dynamic_callback_token(
                    parts[2], "group_character", chat_id, db=db
                )
                data = f"groupchars:{parts[1]}:{resolved or ''}"
        handle_group_panel_callback(
            db, token, chat_id, session, data, message, operation_id,
            sender_id=sender,
            request_context=request_context,
        )
        return

    handle_provider_model_callback(
        db, token, callback, callback_answer, data, chat_id, message,
        session, session_id, operation_id,
        request_context=request_context,
    )
```

- [ ] **Step 3: Remove orchestration-only imports and `process_callback()` from `bridge/callbacks.py`**

Keep `close_panel_message`, `discard_panel_binding`, and `is_session_scoped_panel_callback` in `bridge.callbacks`. Remove imports used only by `process_callback`, especially `bridge.panel_callback_routes`.

- [ ] **Step 4: Point the durable worker at the canonical owner**

```python
# bridge/worker_orchestration.py
from bridge.callback_dispatch import process_callback
```

Remove `from bridge.callbacks import process_callback`.

### Task 3: Migrate tests to the canonical dispatcher

**Files:**
- Modify: `tests/test_alternate_greetings.py`
- Modify: `tests/test_audit_regressions.py`
- Modify: `tests/test_catalog_limits.py`
- Modify: `tests/test_character_session_chain.py`
- Modify: `tests/test_composition.py`
- Modify: `tests/test_group_turn_gating.py`
- Modify: `tests/test_help_drilldown.py`
- Modify: `tests/test_hindsight_session_cleanup.py`
- Modify: `tests/test_item_panel_layouts.py`
- Modify: `tests/test_note_panel.py`
- Modify: `tests/test_operation_recovery.py`
- Modify: `tests/test_panel_context_ownership.py`
- Modify: `tests/test_panel_expiry_feedback.py`
- Modify: `tests/test_panel_lifecycle.py`
- Modify: `tests/test_pdf_worker.py`
- Modify: `tests/test_reset_behavior.py`
- Modify: `tests/test_session_delete.py`
- Modify: `tests/test_session_naming.py`
- Modify: `tests/test_start_onboarding.py`
- Modify: `tests/test_sync_audit.py`
- Modify: `tests/test_world_management.py`
- Modify: `tests/test_runtime_architecture_guards.py`
- Modify: `tests/test_application_import_boundaries.py`

**Interfaces:**
- Consumes: `bridge.callback_dispatch.process_callback` from Task 2.
- Produces: tests that patch the real owner rather than preserving an alias in `bridge.callbacks`.

- [ ] **Step 1: Add the canonical dispatcher import where direct callback tests need it**

```python
import bridge.callback_dispatch as _m_callback_dispatch
```

For `tests/test_panel_context_ownership.py`, use:

```python
import bridge.callback_dispatch as callback_dispatch
```

Keep `bridge.callbacks` imports where tests exercise lifecycle primitives such as `close_panel_message()` or `is_session_scoped_panel_callback()`.

- [ ] **Step 2: Migrate accidental `bridge.callbacks` re-exports to canonical owners**

Use this mapping everywhere in `tests/`; do not keep unused imports in `bridge.callbacks` just to satisfy old test paths:

- `ensure_session` -> `bridge.telegram.ensure_session`
- `DEFAULT_MODEL` -> `bridge.config.DEFAULT_MODEL`
- `answer_callback` -> `bridge.catalog.answer_callback` for direct handler tests, or `bridge.callback_dispatch.answer_callback` when intercepting `process_callback()`
- `handle_group_panel_callback` -> `bridge.groups.handle_group_panel_callback`
- `panel_owner_for_message` / `panel_session_for_message` -> `bridge.database`
- `send_text` -> `bridge.telegram.send_text` for direct use, or `bridge.callback_dispatch.send_text` when intercepting dispatcher behavior
- `process_callback` -> `bridge.callback_dispatch.process_callback`

After migration, `bridge.callbacks` should expose only its actual lifecycle API plus dependencies required by those lifecycle functions.

- [ ] **Step 3: Replace direct calls and patches of the old dispatcher owner**

Examples:

```python
_m_callback_dispatch.process_callback(
    db,
    "injected-token",
    callback,
    services=self.services,
)
```

```python
with patch.object(
    _m_callback_dispatch,
    "handle_primary_panel_callback",
    side_effect=lambda *_args, **kwargs: captured.update(kwargs) or True,
):
    _m_callback_dispatch.process_callback(
        db,
        "injected-token",
        callback,
        services=self.services,
    )
```

Move every name resolved by `process_callback()` to the dispatcher patch target, including `answer_callback`, `send_text`, `handle_*_panel_callback`, and imported aliases such as `close_panel_message` when a test stubs the helper itself. Keep patches for lifecycle-helper internals on their canonical module; for example, patch `bridge.callbacks.telegram_request` when testing the real `close_panel_message()` implementation.

- [ ] **Step 4: Update architecture ownership expectations**

In `tests/test_application_import_boundaries.py`, add `"callback_dispatch.py"` to `MIGRATED_RUNTIME_FILES`, import `bridge.callback_dispatch`, and change the owner tuple from:

```python
(callbacks, ("process_callback",)),
```

to:

```python
(callback_dispatch, ("process_callback",)),
```

In `tests/test_runtime_architecture_guards.py`, import `process_callback` from `bridge.callback_dispatch`, and change the required-service source scan from `callbacks.py` to `callback_dispatch.py`.

- [ ] **Step 5: Run focused behavior and architecture tests**

Run:

```bash
pytest -q \
  tests/test_callback_dispatch_boundary.py \
  tests/test_composition.py tests/test_panel_context_ownership.py \
  tests/test_panel_expiry_feedback.py tests/test_note_panel.py \
  tests/test_panel_lifecycle.py tests/test_group_turn_gating.py \
  tests/test_help_drilldown.py tests/test_session_naming.py \
  tests/test_runtime_architecture_guards.py \
  tests/test_application_import_boundaries.py
```

Expected: all selected tests pass.

### Task 4: Verify the graph cut and exact branch head

**Files:**
- No production changes expected.

**Interfaces:**
- Consumes: completed PR-A tree.
- Produces: evidence that the cycle cut is real and no new cyclic module replaced it.

- [ ] **Step 1: Measure the import graph**

Run this dependency-free SCC check from repository root:

```bash
python - <<'PY'
import ast
from pathlib import Path
root = Path("bridge")
mods = {p.stem: p for p in root.glob("*.py") if p.name != "__init__.py"}
edges = {name: set() for name in mods}
for name, path in mods.items():
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        targets = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
        for target in targets:
            if target.startswith("bridge."):
                peer = target.split(".", 1)[1].split(".", 1)[0]
                if peer in mods:
                    edges[name].add(peer)
index = 0; stack = []; on = set(); idx = {}; low = {}; comps = []
def visit(v):
    global index
    idx[v] = low[v] = index; index += 1; stack.append(v); on.add(v)
    for w in edges[v]:
        if w not in idx: visit(w); low[v] = min(low[v], low[w])
        elif w in on: low[v] = min(low[v], idx[w])
    if low[v] == idx[v]:
        comp = []
        while True:
            w = stack.pop(); on.remove(w); comp.append(w)
            if w == v: break
        comps.append(comp)
for v in edges:
    if v not in idx: visit(v)
cyclic = [c for c in comps if len(c) > 1]
print("largest_scc=", max(map(len, cyclic), default=0))
print("cyclic_modules=", sum(map(len, cyclic)))
PY
```

Expected on the PR-A branch from the approved base: `largest_scc=31`, `cyclic_modules=31`.

- [ ] **Step 2: Run static verification**

```bash
python -m compileall -q bridge tests
git diff --check
```

Expected: both commands exit 0.

- [ ] **Step 3: Run the full suite on the exact working tree**

```bash
pytest -q -n auto
```

Expected baseline count: `728 passed, 464 subtests passed` plus the newly added boundary tests; no failures.

- [ ] **Step 4: Commit the implementation**

```bash
git add bridge/callback_dispatch.py bridge/callbacks.py bridge/worker_orchestration.py \
  tests/test_callback_dispatch_boundary.py tests/test_alternate_greetings.py \
  tests/test_audit_regressions.py tests/test_catalog_limits.py \
  tests/test_character_session_chain.py tests/test_composition.py \
  tests/test_group_turn_gating.py tests/test_help_drilldown.py \
  tests/test_hindsight_session_cleanup.py tests/test_item_panel_layouts.py \
  tests/test_note_panel.py tests/test_operation_recovery.py \
  tests/test_panel_context_ownership.py tests/test_panel_expiry_feedback.py \
  tests/test_panel_lifecycle.py tests/test_pdf_worker.py \
  tests/test_reset_behavior.py tests/test_session_delete.py \
  tests/test_session_naming.py tests/test_start_onboarding.py \
  tests/test_sync_audit.py tests/test_world_management.py \
  tests/test_runtime_architecture_guards.py tests/test_application_import_boundaries.py
git commit -m "refactor: separate callback dispatch boundary"
```

- [ ] **Step 5: Publish to the shared fork and open the PR**

Branch name: `refactor/callback-dispatch-boundary`

PR title: `Refactor callback dispatch boundary`

PR description must include the exact head SHA, focused/full-suite results, and before/after import metrics (`32 -> 31`). Keep the worktree until review and merge are complete.

- [ ] **Step 6: Verify GitHub CI on the exact PR head**

Require both the normal test workflow and dependency audit to succeed before calling the PR ready for review.

### Task 5: Refresh after the sibling PR merges

- [ ] Fetch current `origin/main` after either first-wave PR merges.
- [ ] Rebase or merge only if needed; prefer a normal fast-forwardable update and never plain `--force`.
- [ ] Re-run the focused tests, graph measurement, static checks, and full suite on the refreshed exact head.
- [ ] Confirm the combined `main` expectation is `largest_scc <= 30` before the second first-wave PR is merged.
