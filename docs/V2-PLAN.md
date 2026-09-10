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
2. Prepare production build and macOS system LaunchDaemons for frontend/backend/tunnel and sleep prevention; use an installation location outside Desktop privacy restrictions, bounded logs, automatic restarts and private configuration.
3. Install/start services where host privileges and tunnel credentials permit. Supply equivalent systemd instructions if deployment host differs.
4. Verify health, authenticated API through proxy, process restart and daemon configuration. Document backup/restore, updates, recovery, credential bootstrap and machine/power/FileVault limitations. Commit.

## Completion checks

Run backend suite, database isolation tests, frontend typecheck/build and dependency security review. Inspect commits and ensure secrets are excluded. Record completed verification and any external blocker precisely; do not claim logout/reboot resilience without system services actually installed.

## Progress

- Repository and all three primary design screens inspected; current host is macOS ARM64.
- Existing user modification to .gitignore will be preserved.

- Checkpoints 1–2 implemented: additive Neon migration applied; real isolated PostgreSQL tests pass (27 tests covering accounts, legacy API contracts, all five stored modes, authorization and artifact bytes). PostgreSQL test schemas use direct connections because the transaction pooler rejects search_path startup options.
- Dependency review identified vulnerable legacy framework/PDF versions; upgraded Python dependencies and Next.js/React, removing the unused shadcn CLI dependency. Frontend audit now reports zero vulnerabilities.
