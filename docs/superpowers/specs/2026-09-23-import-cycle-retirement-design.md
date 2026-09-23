# Import-Cycle Retirement Design

Date: 2026-09-23
Status: proposed for implementation
Base: `main` at `4c187043d5fa9275b8410ce9fc00e194da5405a9`

## 1. Purpose

Retire the remaining late-import dependency cycles without changing Telegram behavior, persistence semantics, or service composition.

The previous runtime/facade migration is complete. The remaining structural debt is now dependency direction: ordinary modules still rely on late imports to survive a large mutually dependent application/UI cluster.

This repository is preproduction. The design does not preserve obsolete compatibility wrappers or legacy import surfaces merely for backward compatibility.

## 2. Current evidence

A static AST import-graph scan of merged `main` found:

- 68 `bridge` modules;
- 414 internal `bridge.*` import edges;
- one strongly connected component containing 32 modules;
- 25 direct reciprocal module pairs;
- many modules still ending with `# Explicit late imports replace transitional dependency injection.`

The objective is not to remove late imports mechanically. A change is valuable only when it establishes a clearer ownership direction and does not increase cyclic coupling.
## 3. Goals

1. Make dependency direction explicit and ordinary-import safe.
2. Reduce the large cyclic component incrementally with small reviewable PRs.
3. Preserve runtime behavior and current public Telegram commands.
4. Remove obsolete late-import seams as their ownership is corrected.
5. Add architecture guards so retired cycle edges cannot return.
6. Keep each implementation PR independently testable and mergeable.

## 4. Non-goals

- no broad dead-code purge;
- no provider/generation rewrite;
- no database redesign;
- no new compatibility facade or service locator;
- no `route_update()` decomposition during the first import-cycle wave;
- no requirement to eliminate all 32 cyclic modules in one change.

`route_update()` decomposition is a follow-up after import topology is cleaner.

## 5. Approaches considered

### A. Big-bang import normalization

Rewrite the full 32-module component in one PR. Rejected because review surface and regression risk would be excessive, and failures would be hard to localize.

### B. Remove reciprocal imports opportunistically

Pick visually obvious `A <-> B` pairs and move functions until the direct pair disappears. Rejected as the primary strategy because several tested pairs remove reciprocity without reducing the actual SCC.
### C. Graph-guided incremental edge retirement — selected

Use the import graph to identify edges whose removal measurably reduces the largest SCC, then choose ownership moves that eliminate those edges without adding replacement cycles.

Each PR records before/after graph metrics. The first wave uses two file-disjoint cuts so development and review can proceed in parallel.

## 6. Architecture rule

Lower-level utilities and state owners must not import higher-level routing/orchestration modules merely to reach a callback or Telegram action.

When a cycle exists because orchestration and primitives share one module, move orchestration upward into a dedicated dispatcher or move the primitive into the module that naturally owns the side effect. Do not solve cycles with dynamic lookup, global registries, new facades, or import-time mutation.

Late imports may remain temporarily only where their cycle has not yet been addressed. A PR that retires an edge must remove the late-import requirement for that edge and add a regression guard.

## 7. First wave: parallel PRs

### PR A — callback dispatch boundary

Current problem:

`callbacks.py` owns low-level panel lifecycle helpers but also owns `process_callback()`, which imports `panel_callback_routes.py`. `panel_callback_routes.py` imports the panel lifecycle helpers back from `callbacks.py`.

Selected ownership:

- `callbacks.py`: panel lifecycle primitives such as close/discard binding helpers;
- new `callback_dispatch.py`: callback orchestration and session/owner validation;
- `panel_callback_routes.py`: concrete callback-route handlers;
- worker orchestration calls `callback_dispatch.process_callback()`.
Required result:

- `callbacks.py` no longer imports `panel_callback_routes.py`;
- `panel_callback_routes.py` may continue importing callback lifecycle primitives;
- no replacement import cycle is introduced through `callback_dispatch.py`;
- callback behavior, queued callback acknowledgement, panel ownership checks, and session binding remain unchanged;
- architecture tests forbid `callbacks -> panel_callback_routes` from returning.

Graph expectation on the current base: removing this edge alone reduces the largest SCC from 32 modules to 31.

### PR B — pending Telegram prompt ownership

Current problem:

`common.py` is foundational runtime support but `delete_pending_input_prompts()` locally imports `telegram_request()` from `telegram.py`. `telegram.py` already imports `common.py`, creating a foundational-to-adapter back edge.

Selected ownership:

- move `delete_pending_input_prompts()` to `telegram.py`;
- `input_flows.py` and `session_naming.py` import it from `telegram.py` directly;
- `common.py` stops importing Telegram code entirely for this behavior;
- no compatibility wrapper remains in `common.py`.

This does not create a new module or new graph edge because both callers already depend on `telegram.py`.

Required result:

- no production import from `common.py` to `telegram.py`;
- pending prompt deletion behavior and error handling remain unchanged;
- architecture tests forbid the retired edge;
- no compatibility re-export is added to `common.py`.

Graph expectation on the current base: removing this edge alone reduces the largest SCC from 32 modules to 31.
## 8. Parallel execution and merge discipline

PR A and PR B branch from the same verified base and have disjoint production file sets, so implementation may proceed concurrently in separate worktrees.

Expected graph effect on the current base:

- base largest SCC: 32;
- PR A alone: 31;
- PR B alone: 31;
- both cuts applied: 30.

After the first PR merges, the second branch must be refreshed onto current `main`, its graph metric recomputed, and the full suite rerun before merge. No history rewrite is required unless GitHub cannot fast-forward the branch; if a rewrite ever becomes necessary, use only `--force-with-lease` after exact remote-SHA verification.

Parallel work stops if either PR discovers a need to edit the other PR's production files. In that case, preserve the clearer ownership change and serialize the second PR.

## 9. TDD and architecture guards

Each implementation PR starts with a failing architecture test that proves the forbidden edge still exists on its base.

PR A guard must prove:

- `bridge.callbacks` does not import `bridge.panel_callback_routes`;
- callback dispatch remains callable from its canonical new owner;
- importing lifecycle primitives does not import the route module as a side effect.

PR B guard must prove:

- `bridge.common` does not import `bridge.telegram`;
- `delete_pending_input_prompts` is owned by `bridge.telegram`;
- `input_flows` and `session_naming` use the canonical owner.

Behavior tests must cover the moved functions before production edits are accepted.
## 10. Verification requirements

For every PR:

1. focused tests for the moved behavior;
2. architecture/import-boundary tests;
3. `python -m compileall -q bridge tests`;
4. `git diff --check`;
5. full pytest-xdist suite on the exact branch head;
6. GitHub CI success on the exact PR head;
7. import-graph measurement recorded in the PR description.

No PR is considered complete merely because the direct reciprocal import disappears. The resulting graph must not gain a new cyclic module or a larger SCC.

## 11. Second wave

After both first-wave PRs merge, recompute the import graph from current `main` before choosing the next cuts.

Likely candidates are the Telegram transport/session cluster and remaining callback/message routing cycles, but the next PR boundaries are selected from the new graph rather than frozen from the old one.

The intended Telegram direction is:

`transport primitives -> Telegram adapter/session operations -> UI/application routing`

Higher-level UI modules should not be required by the low-level transport owner. Any transport extraction must migrate callers to the canonical owner rather than create a permanent compatibility facade.

The next wave may use parallel PRs only when the graph and changed-file sets prove they are independent.

## 12. Route-update follow-up

`update_routing.route_update()` is currently a large dispatcher and is a legitimate later decomposition target. It remains out of the first import-cycle implementation plan.

After the import graph has been reduced and dependency ownership is clearer, decompose update routing by update type (callback, edit, media/document, command/message) while preserving durable-job semantics and processed-update accounting.
## 13. Acceptance criteria

First-wave implementation is complete when:

1. PR A and PR B are merged from the shared `punzer4-code` fork.
2. The combined import graph has no more than 30 modules in its largest SCC on the measured base lineage.
3. The retired edges `callbacks -> panel_callback_routes` and `common -> telegram` are guarded against regression.
4. No new compatibility wrapper, dynamic lookup bridge, or service locator was added.
5. Callback behavior and pending-prompt deletion behavior are unchanged.
6. Full local and GitHub CI suites pass on exact PR heads.
7. Current `main` is remeasured before a second-wave design is selected.

## 14. Risks and controls

**Import-order regressions.** Moving orchestration can expose accidental import-time assumptions. Control: standalone import tests plus full-suite verification.

**Patch-target drift in tests.** Tests may patch symbols from their old owner. Control: migrate tests to the canonical owner rather than adding aliases solely for tests.

**Parallel-branch conflict.** A hidden dependency may require overlapping files. Control: stop parallel execution and serialize rather than forcing a conflict-prone split.

**Metric gaming.** A new module could replace an ejected cyclic module and leave the SCC unchanged. Control: record both largest-SCC size and total cyclic-module count, and reject changes that merely rename the cycle.

## 15. Decision

Use graph-guided incremental retirement. Execute the first two metric-positive, file-disjoint cuts in parallel. Recompute after merge, then choose the next wave from evidence. Decompose `route_update()` only after the import-cycle work has established cleaner dependency directions.
