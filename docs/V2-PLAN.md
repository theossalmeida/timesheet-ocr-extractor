# AUTUS v2 implementation plan

## Scope and decisions

Implement the supplied AUTUS, Login and Equipe designs in the existing Next.js app. Preserve all five extraction modes. Replace prototype data with authenticated team data. Store original PDFs and generated Excel/CSV/ZIP bytes in Neon PostgreSQL; do not migrate nonexistent historical files. Use email/password with Argon2id, opaque revocable sessions in HttpOnly cookies, CSRF protection, and invitation tokens bound to email. Registration requires a private bootstrap code or invitation; authenticated users can create teams. Team creators are administrators, members share history, and removal preserves documents. Never infer billing amounts from prototype examples: unknown external AI costs remain explicitly unavailable.

Use one same-origin API proxy and a single persistent tunnel, with backend bound to loopback. Prefer small connection pools, bounded upload reads, serialized extraction, indexed metadata queries, and separate file contents so history never loads blobs. All work follows plan, implementation, review/fix, verification, then checkpoint commit.

## Checkpoint 1: Persistence and authentication

1. Inspect secret configuration without printing credentials; set protected local environment.
2. Add versioned PostgreSQL migrations, pooled connections, users, sessions, teams, memberships, invitations, extraction metadata, and binary artifacts.
3. Implement registration/login/logout/password changes, session expiry/revocation, CSRF, bounded validation and login throttling.
4. Implement team creation/switching, admin invitations/revocation/removal, last-admin protection, and token acceptance.
5. Verify authentication, expired/reused invitations, authorization and cross-team denial with isolated database integration tests. Apply additive migration to Neon and commit reviewed code.

## Checkpoint 2: Durable extraction and history

1. Authenticate every extraction/preview endpoint and scope uploads to membership.
2. Persist input before processing and all generated artifacts atomically before success. Record failure/interruption without exposing internal errors.
3. Add team history/search/pagination and authorized original/output downloads; keep blobs out of list queries.
4. Bound upload/processing resource use; retain streaming progress and existing formats.
5. Verify all modes, failure states, persistence and cross-team download denial; run existing extraction tests and commit.

## Checkpoint 3: AUTUS frontend

1. Implement the supplied typography, tokens, square blueprint frames, navigation and responsive two-column extraction/history layout.
2. Implement email/password login, registration/invitation acceptance, team creation/switching and logout.
3. Connect upload/progress/results, history search/downloads, truthful monthly usage, team members and invitation management to APIs.
4. Review keyboard access, pending/error/empty states, credential handling and mobile layout; typecheck and production build; commit.

## Checkpoint 4: Home server operation

1. Identify the actual host, privileges, existing tunnel credentials and service tooling.
2. Prepare production build and Windows services for frontend/backend/tunnel and sleep prevention; use an installation location outside Desktop privacy restrictions, bounded logs, automatic restarts and private configuration.
3. Install/start services where host privileges and tunnel credentials permit. Target confirmed by the user: a separate Windows 11 home server.
4. Verify health, authenticated API through proxy, process restart and daemon configuration. Document backup/restore, updates, recovery, credential bootstrap and machine/power/FileVault limitations. Commit.

## Completion checks

Run backend suite, database isolation tests, frontend typecheck/build and dependency security review. Inspect commits and ensure secrets are excluded. Record completed verification and any external blocker precisely; do not claim logout/reboot resilience without system services actually installed.

## Progress

- Repository and all three primary design screens inspected; development host is macOS ARM64; production is Windows 11.
- Existing user modification to .gitignore will be preserved.

- Checkpoints 1–2 implemented: additive Neon migration applied; real isolated PostgreSQL tests pass (27 tests covering accounts, legacy API contracts, all five stored modes, authorization and artifact bytes). PostgreSQL test schemas use direct connections because the transaction pooler rejects search_path startup options.
- Dependency review identified vulnerable legacy framework/PDF versions; upgraded Python dependencies and Next.js/React, removing the unused shadcn CLI dependency. Frontend audit now reports zero vulnerabilities.

- Checkpoint 3 complete: production frontend build and real Chromium workflow passed, including login, actual PDF extraction, Excel download, history after reload, invitation signup and mobile overflow/member permissions.
- Added 8 MiB chunk uploads and independent background jobs to support 200 MiB documents through Cloudflare without relying on SSE. Three focused tests cover chunk integrity, team boundaries, idempotent starts and completion after logout.
- Full backend suite before the chunked addition: 215 passed, 1 skipped.
- User confirmed production host is Windows 11, with timesheet.theosantoro.dev and api.theosantoro.dev. Awaiting remote access and existing Cloudflare configuration paths.

- Storage scope changed at user request: private Cloudflare R2 bucket `autus`, raw_files and processed_files prefixes. Neon now stores object references and metadata; backward-compatible migration supports moving existing binary rows to R2. Nine real R2 integration tests passed; all test objects removed. R2 public development domain is disabled and no public custom domains exist.
- Updated suite: 218 passed, 2 skipped (optional browser test plus one existing fixture test).
- Windows access established through Tailscale SSH after user enabled OpenSSH. Existing app runs from C:\Projetos\Timesheet Extractor in interactive processes. Existing named tunnel configuration found; production configuration preserves its routes and OCR settings.

- Production switched to Windows services (LocalService, service-specific secret ACLs), reusing the existing named Cloudflare tunnel and preserving the old project files. Public health returns v2; both hostnames reject anonymous document requests.
- Public Chromium verification passed against the actual Windows/R2/Neon deployment: account login, PDF processing, Excel download, identical original bytes, history reload, mobile layout, no JavaScript errors. Synthetic verification document was removed afterward.
- Initial account and team provisioned; generated password stored only in a restricted Windows first-login file.
- Production backup command tested; Python dependency audit and npm audit report zero known vulnerabilities.

- All three Windows services recovered automatically after their application process trees were terminated. Verified new service process IDs, session 0, LocalService account, automatic startup, loopback-only app listeners, and working public login/history afterward. No forced desktop logout or machine reboot was performed.
- Old start/stop batch files now control only AUTUS services; originals are retained as .v1.bak. No legacy AUTUS scheduled startup tasks were found.
- Implementation complete; operations guide and repeatable Windows preparation/activation scripts included.

- Gemini calls are now metered individually: each generateContent response's usageMetadata is recorded (prompt, cached, output and thinking tokens), priced per token with litellm's model price map - which follows Google's published tiers, including the higher rate above a 200k-token prompt and the cached-input discount - and converted with the day's USD/BRL quote, cached for 24h with a configured fallback. One ai_usage row is stored per HTTP call, including chunk retries and responses that failed to parse, since Google bills those too; `extractions.cost_brl` is their sum. Unknown remains unknown: an unmapped model, a response without usage metadata or an unavailable exchange rate leaves the document cost null instead of understating it.
