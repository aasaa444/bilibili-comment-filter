# Implementation Architecture

This document turns the accepted product specification into bounded code ownership. It is
implementation guidance, not a replacement for `docs/specs/bilibili-comment-filter.md`.

## Runtime boundaries

```text
Chromium extension
  popup + content script + service worker
            | localhost HTTP
            v
FastAPI service
  API adapters -> application services -> SQLite repositories
                              |              |
                              |              +-- UID cache/version authority
                              +-- task worker -> collector -> batch analyzer
                                           |
                                           +-- blacklist queue -> headless Chromium executor
```

- `service/` owns domain state, persistence, orchestration, external adapters and HTTP routes.
- `extension/` owns Manifest V3 permissions, current-video discovery, local UID filtering and
  popup/service-worker communication. It never becomes the authority for UID state.
- `web/` owns the management page and consumes the FastAPI HTTP API; it does not read SQLite.
- `shared/` contains browser-safe API DTOs and small pure client helpers only.
- `tests/` tests public seams. The primary orchestration seam is `TaskOrchestrator`; adapters are
  replaced with fixed test doubles for unit and integration tests.

## Core public seams

- `CommentCollector.collect(task, checkpoint)` returns pages, replies, pinned comments, coverage
  and a resumable checkpoint.
- `BatchAnalyzer.analyze(accounts, samples)` returns validated `hit`, `non_target` or `uncertain`
  results with evidence references and sample/rule versions.
- `TaskOrchestrator.run(task_id)` coordinates collection, grouping, analysis, registry updates,
  evidence persistence and queue creation idempotently.
- `UidRegistry` owns global UID states and cache versions.
- `BlacklistExecutor.execute(item)` is the only boundary allowed to perform the visible native
  Bilibili blacklist action. The test executor records UIDs and never opens a real account.

## Persistence ownership

SQLite is authoritative. The first schema keeps the tables conceptually separate even when a
repository implementation shares a module:

- `video_tasks`, `task_checkpoints`, `comments`, `evidence`
- `uid_records`, `uid_events`, `sync_versions`
- `sample_sets`, `sample_items`
- `review_actions`
- `blacklist_queue`
- `auth_sessions`

All mutating operations must be idempotent on their stable external keys. The extension receives
versioned UID snapshots/deltas and may continue filtering from its last successful cache while the
service is unavailable.

## HTTP contract

The initial API is intentionally small and observable:

- `GET /api/health`
- `GET/POST /api/auth/session`
- `GET /api/uids`, `POST /api/uids`, `PATCH /api/uids/{uid}`
- `GET/POST /api/tasks`, `GET /api/tasks/{task_id}`, `POST /api/tasks/{task_id}/retry`
- `GET /api/tasks/{task_id}/comments`
- `GET/POST /api/reviews`, `POST /api/reviews/{evidence_id}`
- `GET /api/review-actions`
- `GET/POST /api/samples`, `POST /api/samples/{sample_id}/publish`
- `GET /api/blacklist`, `POST /api/blacklist/{item_id}/pause`, `/resume`, `/retry`
- `GET /api/uids/sync?since=<version>`

The service must return truthful connection and state errors. It must not return a ready/connected
status when the backing store or worker is unavailable.

## Dependency order

1. Health, database lifecycle and typed API error envelope.
2. UID registry, versioned cache sync and the extension's offline filtering.
3. Video task creation/state display and session diagnostics.
4. Collector protocol plus fixed-response collection of root/reply/pinned comments.
5. Batch analyzer protocol, rule/sample injection and structured output validation.
6. Orchestration, evidence and review actions.
7. Blacklist queue state machine, test executor and headless native executor.
8. Windows/Docker startup and end-to-end acceptance.

## Resolved implementation decisions

- Queue items are auto-consumed by the single background worker after creation. `pause` and
  `resume` are operator controls for the whole queue/item; they are not per-item approval gates.
- UID status and blacklist queue status are separate fields. A UID may be `hidden` while its queue
  item is `queued`, `paused`, `failed` or `blocked`; queue transitions must not overwrite UID
  review state.
- Collection keeps task-scoped raw comments and coverage data until the task reaches a terminal
  state. Long-term evidence is created only for `hit` and `uncertain` decisions; non-target raw
  comments are not exposed as global evidence and may be cleaned by a later retention policy.
- Posting the same normalized video while an unfinished task exists returns that task. A future
  explicit rerun flag may create a new task; the first implementation does not silently duplicate
  active work.
- Coverage uses the source-declared total when present and records its denominator kind. When the
  source does not declare a total, the task reports observed-page coverage rather than inventing a
  percentage.
- Rule-only seed examples are available before user sample versions exist. The built-in provider
  is versioned and is replaced/augmented by the published sample set for later tasks.
- Session handoff never logs raw cookies. The local store may persist the session needed by the
  worker, but API responses expose only authentication status, timestamps and diagnostic reasons.
- Docker runs Chromium headless. Windows local mode also uses a hidden/headless worker process;
  neither mode opens a visible browser window or captures keyboard/mouse input.
