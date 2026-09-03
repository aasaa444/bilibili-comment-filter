# Ticket completion audit

Date: 2026-08-09

This audit records the implementation evidence for the remaining GitHub tickets. The
repository's authoritative behavior is the local SQLite service, the extension cache, and
the replaceable external adapters; live Bilibili, model-provider, and native blacklist
actions are not substituted with fake success in the production adapters.

## Verification evidence

- `.venv\Scripts\python.exe -m pytest -q`: 159 passed.
- `npm test`: 42 passed.
- `npm run build`: passed.
- `npm run typecheck`: passed.
- `ruff check .`: passed.
- `docker compose config --quiet`: passed.
- `git diff --check`: passed.
- Published nickname-positive samples are applied as deterministic hard rules before
  model calls; when the remote model is unconfigured, those rule results still reach
  evidence, UID state, and the blacklist queue while the task remains `partial`.
- Review `revoke` now removes the UID from the authoritative registry and emits a
  delta-sync removal; `exception` remains a persistent future-match override, and a
  cancelled blacklist item can be queued again after a later detection.
- The local Compose container was healthy and `GET /api/health` returned `status: ready`; the
  response also exposed redacted model configuration flags (`base_url_configured`,
  `model_configured`, `api_key_configured`) without returning endpoint, model-name, or key values.
- The rebuilt Compose image was started with `docker compose up -d --no-build`; the container
  health probe remained healthy, and the `rules-v2` fallback probe confirmed standalone hits for
  `巴斯特`, `䟋`, `天龙八部`, and `粘慕斯`, while `曼巴斯特` remained a friendly exception.
- A live read-only probe against `BV1z2uH6XEbC` through the updated container saved 8 root
  comments and 19 replies (27 comments total, coverage `1.0`); the task remained `partial` only
  because the remote model is intentionally unconfigured, with no fake analysis result produced.
- The installed extension was verified on a real Bilibili video page: UID `350213094` was
  hidden while unrelated comments remained visible.
- A read-only real-session handoff probe also passed: the current Chrome session was a
  logged-in Bilibili video page, the latest persisted auth session had `source=extension`
  and `status=valid`, and an independent headless Chromium launched by the service used
  that session to reach `https://space.bilibili.com/350213094` with HTTP 200 and the
  expected `HKbelong2CHN` page title. No raw Cookie value was emitted and no account
  mutation was attempted.
- The extension background auth-sync boundary is covered by fixture tests: startup/installation
  and the periodic cache alarm reuse a Bilibili tab when present, and skip cookie reads otherwise.

## Ticket mapping

| Ticket | Scope | Implementation evidence | Test evidence |
| --- | --- | --- | --- |
| #6 / T05 | Login-session synchronization and diagnostics | `extension/src/service-worker.ts`, `service/auth.py`, `service/app.py`, `web/src/main.ts` | `tests/frontend/service-worker.test.mjs`, `tests/frontend/auth-diagnostic.test.mjs`, `tests/service/test_auth.py`, `tests/service/test_health_auth.py` |
| #7 / T06 | First-level comment collection | `service/collector.py`, `service/orchestrator.py`, task/comment persistence and API routes | `tests/service/test_collector.py` (fields, retry idempotency, empty-page records), `tests/service/test_orchestrator.py`, `tests/e2e/test_comment_filter_flow.py` |
| #8 / T07 | Replies, pinned comments, coverage, and resume | `CollectionCheckpoint`, cursor/page handling, reply relation persistence, task progress rendering | collector checkpoint/reply/cursor cases and restart-resume cases in `tests/service/test_collector.py`, `tests/service/test_tasks.py`, and `tests/service/test_orchestrator.py` |
| #9 / T08 | Batch AI analysis and structured results | `service/analyzer.py`, `service/samples.py`, `service/orchestrator.py` | `tests/service/test_analyzer.py`, `tests/service/test_orchestrator.py` |
| #15 / T14 | Native headless blacklist executor | `service/blacklist.py`, `service/worker.py`, configurable headless Chromium, explicit pause/error classification | `tests/service/test_blacklist.py`, `tests/service/test_worker.py`, `tests/service/test_management_api.py` |
| #16 / T15 | Windows start, Docker Compose, and end-to-end acceptance | `Dockerfile`, `docker-compose.yml`, `scripts/start.ps1`, `scripts/stop.ps1`, `tests/e2e/test_startup_contract.py` | startup contract tests, Compose validation, health check, and the full test suites above |

Issue #1 is the parent specification and can be closed after the child tickets above are
closed.

## Current open-ticket boundary

- #6 is complete for the real-session handoff boundary: Chrome -> local auth sync ->
  independent Chromium navigation was demonstrated without an account mutation. The
  native UI action itself remains covered by #15 and was intentionally not executed.
- #7/#8 are partially verified: the transport now follows the current WBI-signed Bilibili web comment
  endpoint and carries opaque `pagination_str` cursors. A real smoke run on `BV1eFu36LEt2`
  returned two distinct 20-item root pages, but full real comment-area convergence has not
  yet been demonstrated, so the service must still keep the task `partial` rather than
  treating a platform terminal response as proof of complete collection. Across checkpoint
  retries, previously recorded failure items are retained and de-duplicated instead of being
  overwritten by the latest collection attempt.
- A read-only real-session run on `BV1z2uH6XEbC` reached `complete=true` with 8 root comments,
  19 replies, 100% reported coverage, and one explainable terminal empty-page diagnostic after
  the current `top` metadata wrapper fix. A subsequent larger read-only run on `BV1eFu36LEt2`
  observed 206 roots and 604 replies against an all-level declaration of 851 and reply
  declarations of 627, reported `coverage=0.9518`, `complete=false`, `terminal=true`, and
  retained two empty-page diagnostics; it did not write the task database or consume the
  blacklist queue. The WBI `cursor.all_count` value is now treated as the whole-comment total,
  so it is not added to the per-root reply declarations a second time.
- A metadata-only sample of the same live reply protocol showed one root declaring 27 replies
  while returning 19 on its first reply page; this supports retaining the count gap as visible
  incomplete evidence, but is not by itself proof that every missing item is deleted or hidden.
- #9 remains open because the remote OpenAI-compatible endpoint has not yet been configured or acceptance-tested.
- #15 remains open until an explicitly authorized small-scale native blacklist test updates
  a real UID to `blocked`; the test executor does not perform that mutation.
- Native blacklist pacing defaults to a 60-second interval plus 0–30 seconds of random
  jitter, and can be configured through environment variables; this changes scheduling
  conservatism only and does not count as a real Bilibili account mutation.
- #16 and parent #1 remain dependent on those external acceptance boundaries.

## Explicit boundaries

- No raw Cookie value is included in test output, audit text, or API responses.
- No real comment, account blacklist, or other account-state mutation is performed by the
  automated test suite.
- Authentication expiry, captcha, platform interception, and rate-limit responses pause or
  retain resumable work instead of being reported as successful actions.
- The extension scope remains ordinary desktop Bilibili video comments and replies;弹幕、
  live pages, dynamic pages, episodes, mobile, and app surfaces remain out of scope.
