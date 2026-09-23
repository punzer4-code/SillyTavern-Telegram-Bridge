# Pending Telegram Prompt Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move pending-input Telegram prompt deletion from foundational `bridge.common` into the Telegram adapter that owns the side effect.

**Architecture:** `bridge.telegram` becomes the canonical owner of `delete_pending_input_prompts()`. Existing consumers in `bridge.input_flows` and `bridge.session_naming` already depend on `bridge.telegram`, so they import the helper there directly and `bridge.common` loses its only Telegram back-edge for this behavior.

**Tech Stack:** Python 3.11+, unittest/pytest, pytest-xdist, stdlib `ast`/`unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-09-23-import-cycle-retirement-design.md`

## Global Constraints

- Preserve pending-input deletion behavior and error tolerance exactly.
- Do not add a compatibility re-export in `bridge.common`.
- Retire `common -> telegram`; do not create a replacement cyclic dependency.
- Keep the PR independently testable and mergeable from base `4c187043d5fa9275b8410ce9fc00e194da5405a9`.
- Record largest-SCC and total-cyclic-module metrics before and after the change.
- Do not modify callback-dispatch production files in this PR.

## Review Focus

- A list of prompt message IDs must delete every valid ID.
- A scalar integer or string prompt ID must still be normalized to one deletion.
- A malformed or already-unavailable prompt ID must not prevent later valid IDs from being processed.
- Expired/cancelled input flows must still clear their metadata after deleting old prompts.
- Starting a new session-name flow must still clear conflicting prompt messages before sending the new prompt.

---

### Task 1: Add the RED ownership and behavior contract

**Files:**
- Create: `tests/test_pending_prompt_ownership.py`

**Interfaces:**
- Consumes: current `bridge.common`, `bridge.telegram`, `bridge.input_flows`, and `bridge.session_naming`.
- Produces: a permanent ownership guard plus direct behavior coverage for the moved helper.

- [ ] **Step 1: Write the failing ownership tests**

```python
from __future__ import annotations

import ast
from pathlib import Path
import unittest
from unittest.mock import patch

import bridge.telegram as telegram

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


def imported_names(path: Path, module: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == module
        for alias in node.names
    }
```

```python
class PendingPromptOwnershipTests(unittest.TestCase):
    def test_common_does_not_import_telegram(self):
        self.assertNotIn(
            "bridge.telegram",
            imported_modules(BRIDGE_DIR / "common.py"),
        )

    def test_telegram_is_canonical_owner(self):
        import bridge.common as common

        self.assertTrue(callable(telegram.delete_pending_input_prompts))
        self.assertFalse(hasattr(common, "delete_pending_input_prompts"))

    def test_consumers_import_canonical_owner(self):
        for filename in ("input_flows.py", "session_naming.py"):
            path = BRIDGE_DIR / filename
            self.assertIn(
                "delete_pending_input_prompts",
                imported_names(path, "bridge.telegram"),
                filename,
            )
            self.assertNotIn(
                "delete_pending_input_prompts",
                imported_names(path, "bridge.common"),
                filename,
            )
```

- [ ] **Step 2: Add direct behavior coverage for mixed prompt IDs**

```python
    def test_delete_pending_input_prompts_continues_after_bad_id(self):
        calls = []

        def fake_request(_token, method, payload):
            calls.append((method, payload))
            return {}

        with patch.object(telegram, "telegram_request", side_effect=fake_request):
            telegram.delete_pending_input_prompts(
                "token",
                "chat",
                {"prompt_message_ids": [90, "bad", "91"]},
            )

        self.assertEqual(calls, [
            ("deleteMessage", {"chat_id": "chat", "message_id": 90}),
            ("deleteMessage", {"chat_id": "chat", "message_id": 91}),
        ])
```

```python
    def test_delete_pending_input_prompts_accepts_scalar_id(self):
        calls = []
        with patch.object(
            telegram,
            "telegram_request",
            side_effect=lambda _token, method, payload: calls.append((method, payload)) or {},
        ):
            telegram.delete_pending_input_prompts(
                "token", "chat", {"prompt_message_ids": "92"}
            )

        self.assertEqual(calls, [
            ("deleteMessage", {"chat_id": "chat", "message_id": 92}),
        ])
```

- [ ] **Step 3: Run the RED test**

Run: `pytest -q tests/test_pending_prompt_ownership.py`

Expected: FAIL because `delete_pending_input_prompts` is still owned by `bridge.common` and `bridge.telegram` does not expose it.

### Task 2: Move the helper into the Telegram adapter

**Files:**
- Modify: `bridge/telegram.py`
- Modify: `bridge/common.py`
- Modify: `bridge/input_flows.py`
- Modify: `bridge/session_naming.py`

**Interfaces:**
- Consumes: `telegram_request(token, method, payload)` in `bridge.telegram`.
- Produces: `delete_pending_input_prompts(token: str, chat_id: str, state: dict) -> None` from `bridge.telegram`.

- [ ] **Step 1: Add the canonical helper next to Telegram request primitives**

```python
def delete_pending_input_prompts(token: str, chat_id: str, state: dict) -> None:
    """Remove prompt messages created for a pending text-input transition."""
    message_ids = state.get("prompt_message_ids") or []
    if isinstance(message_ids, (int, str)):
        message_ids = [message_ids]
    for message_id in message_ids:
        try:
            telegram_request(
                token,
                "deleteMessage",
                {"chat_id": chat_id, "message_id": int(message_id)},
            )
        except Exception:
            logging.info(
                "Pending input prompt already unavailable",
                exc_info=True,
            )
```

Place it after `telegram_request()` so transport ownership is obvious. `bridge.telegram` already owns `logging` and the request primitive; add no new dependency.

- [ ] **Step 2: Delete the old helper from `bridge/common.py`**

Remove the full `delete_pending_input_prompts()` definition, including its local `from bridge.telegram import telegram_request` import. Do not leave a forwarding wrapper.

- [ ] **Step 3: Point both existing consumers to the canonical owner**

In `bridge/input_flows.py`, remove:

```python
from bridge.common import delete_pending_input_prompts
```

and add the name to the existing Telegram import block:

```python
from bridge.telegram import (
    delete_pending_input_prompts,
    send_panel_request,
    send_text,
    telegram_request,
    update_session,
)
```

In `bridge/session_naming.py`, remove:

```python
from bridge.common import delete_pending_input_prompts
```

and add the helper to its existing Telegram import block:

```python
from bridge.telegram import (
    create_session,
    delete_pending_input_prompts,
    send_text,
    update_session,
)
```

- [ ] **Step 4: Run the new ownership tests**

Run: `pytest -q tests/test_pending_prompt_ownership.py`

Expected: PASS.

### Task 3: Prove existing input-flow behavior did not change

**Files:**
- Existing tests only; no production edits expected.

**Interfaces:**
- Consumes: canonical Telegram-owned deletion helper.
- Produces: regression evidence for expiration, cancellation, and conflicting input cleanup.

- [ ] **Step 1: Run the existing prompt-cleanup behavior tests**

```bash
pytest -q \
  tests/test_panel_lifecycle.py::PanelLifecycleTests::test_settings_invalid_feedback_is_deleted_by_cancel \
  tests/test_note_panel.py::NotePanelTests::test_note_user_input_updates_session_and_expires_state \
  tests/test_note_panel.py::NotePanelTests::test_note_cancel_deletes_text_prompt \
  tests/test_persona_editor.py
```

Expected: all selected tests pass.

- [ ] **Step 2: Add explicit conflicting-input cleanup coverage in the new ownership test file**

Extend `tests/test_pending_prompt_ownership.py` with an isolated SQLite fixture so this PR does not edit `tests/test_session_naming.py` (PR A also migrates that file):

```python
import json
from pathlib import Path
import tempfile
import time

import bridge.database as database
import bridge.session_naming as session_naming

    def test_start_session_name_input_clears_conflicting_prompt_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = database.db_connect(Path(tmp) / "prompt.sqlite3")
            state = {
                "session_id": "default",
                "expires_at": time.time() + 600,
                "prompt_message_ids": [77, 78],
            }
            session_naming.set_meta(db, "settings_input:chat", json.dumps(state))
            seen = []
            with patch.object(
                session_naming,
                "delete_pending_input_prompts",
                side_effect=lambda token, chat_id, value: seen.append((token, chat_id, value)),
            ), patch.object(session_naming, "send_text", return_value=[99]):
                session_naming.start_session_name_input(
                    db,
                    "token",
                    "chat",
                    {"session_id": "default", "model_id": "model"},
                )
            self.assertEqual(seen, [("token", "chat", state)])
            self.assertEqual(session_naming.get_meta(db, "settings_input:chat", ""), "")
            db.close()
```

- [ ] **Step 3: Run focused prompt-flow tests**

Run: `pytest -q tests/test_pending_prompt_ownership.py tests/test_session_naming.py tests/test_panel_lifecycle.py tests/test_note_panel.py tests/test_persona_editor.py`

Expected: all selected tests pass.

### Task 4: Verify the graph cut and exact branch head

**Files:**
- No production changes expected.

**Interfaces:**
- Consumes: completed PR-B tree.
- Produces: exact evidence that `common -> telegram` is gone and the SCC shrank rather than moved.

- [ ] **Step 1: Measure the import graph**

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

Expected on the PR-B branch from the approved base: `largest_scc=31`, `cyclic_modules=31`.

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

Expected baseline count: `728 passed, 464 subtests passed` plus the newly added ownership tests; no failures.

- [ ] **Step 4: Commit the implementation**

```bash
git add bridge/telegram.py bridge/common.py bridge/input_flows.py bridge/session_naming.py \
  tests/test_pending_prompt_ownership.py
git commit -m "refactor: move pending prompt cleanup to telegram"
```

- [ ] **Step 5: Publish to the shared fork and open the PR**

Branch name: `refactor/pending-prompt-ownership`

PR title: `Move pending prompt cleanup to Telegram adapter`

PR description must include the exact head SHA, focused/full-suite results, and before/after import metrics (`32 -> 31`). Keep the worktree until review and merge are complete.

- [ ] **Step 6: Verify GitHub CI on the exact PR head**

Require both the normal test workflow and dependency audit to succeed before calling the PR ready for review.

### Task 5: Refresh after the sibling PR merges

- [ ] Fetch current `origin/main` after either first-wave PR merges.
- [ ] Rebase or merge only if needed; prefer a normal fast-forwardable update and never plain `--force`.
- [ ] Re-run the focused tests, graph measurement, static checks, and full suite on the refreshed exact head.
- [ ] Confirm the combined `main` expectation is `largest_scc <= 30` before the second first-wave PR is merged.
