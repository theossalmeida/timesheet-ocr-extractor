# AUTUS production on Windows 11

AUTUS uses Windows services so the frontend, backend and Cloudflare connector survive logout and start after boot. Production uses https://timesheet.theosantoro.dev. The existing api.theosantoro.dev route can remain; the frontend calls `/api` on its own origin and all backend data routes require authentication.

## Installation

Use an Administrator PowerShell. Required: a current supported 64-bit Python (3.12+), Node.js (22+), Git, Tesseract with Portuguese data, and cloudflared. Install runtimes for the machine or put standalone runtimes under `C:\ProgramData\Autus\runtimes`; services cannot rely on Windows Store execution aliases or a user's PATH.

Configure `backend/.env` using `backend/.env.example`. The installer can also read the Neon connection string from the repository's ignored `secreties.txt`. R2 settings must identify the private `autus` bucket; use object read/write credentials scoped to this bucket. Do not enable public R2 domains. No R2 browser CORS policy is needed because uploads and authorized downloads pass through the backend.

```powershell
.\deploy\install-windows.ps1 `
  -PythonExe 'C:\ProgramData\Autus\runtimes\python\python.exe' `
  -CloudflaredExe 'C:\ProgramData\Autus\runtimes\cloudflared.exe' `
  -TunnelConfigFile 'C:\Users\theoa\.cloudflared\config.yml' `
  -LegacyProjectPath 'C:\Projetos\Timesheet Extractor'
```

Alternatively use `-TunnelTokenFile` for a remotely managed tunnel, or `-UseExistingTunnelService` if Cloudflared is already installed as a Windows service. The installer does not send invitation emails or modify Cloudflare DNS. Configure the named tunnel routes as:

- `timesheet.theosantoro.dev` → `http://127.0.0.1:3000`
- `api.theosantoro.dev` → `http://127.0.0.1:8000`

For a reviewed two-step installation, first run the installer with `-PrepareOnly`, then:

```powershell
.\deploy\activate-windows.ps1 -LegacyProjectPath 'C:\Projetos\Timesheet Extractor'
```

`activate-windows.ps1` installs the prepared definitions, backs up previous service XML, applies service-specific permissions and verifies v2 health. Its optional `-LegacyTunnelProcessId` stops only a verified old `cloudflared tunnel run timesheet` process after the new services are healthy.

`-PrepareOnly` builds a separate release and validates its XML without stopping or installing services. It still writes private configuration and downloads dependencies. A normal install builds before switching; `-LegacyProjectPath` stops only Python/Node processes whose command contains that exact legacy path. Stop the old interactive tunnel only after the service connector is healthy.

Services are `AutusBackend`, `AutusFrontend`, and `AutusTunnel`. They run as LocalService with service-specific permissions for secrets and writable folders, not as LocalSystem. WinSW 2.12.0 is downloaded from its official release and SHA-256 checked. Logs rotate at 10 MB with seven retained files. Startup is automatic with restart on failure. Backend and frontend bind only to loopback; no public port firewall rules are needed.

The installer disables automatic sleep and hibernation while connected to AC power. Display sleep is unaffected. In firmware, enable recovery after AC power loss if supported. A shutdown, forced update/reboot, power/internet outage or disk-unlock prompt still interrupts availability. An always-on computer cannot guarantee uninterrupted service under these conditions.

## First account and invitations

The deployed initial account is `theoalmeida00@gmail.com`, with team `Equipe Theo`. Its generated temporary password is stored in the administrator-only `C:\ProgramData\Autus\config\first-login.txt`; change it under Equipe → Alterar senha. Do not reuse the Windows password.

On a fresh installation, the first account requires the private `BOOTSTRAP_TOKEN` in `C:\ProgramData\Autus\config\backend.env`. Open the app, select “Configurar a primeira conta,” and enter it with your email, name and a password of at least 8 characters. Create a team. The code cannot create further accounts after the first user exists.

Team administrators create invitation links valid for seven days. Share the link privately with the invited email owner. Existing users sign in and accept it; new users register through it. Each account can create its own teams. Removing members immediately revokes their team access while retaining their documents. Administrators cannot be removed. Password changes revoke all other sessions.

For password recovery, use the private server console from the active backend directory:

```powershell
.\.venv\Scripts\python.exe -m scripts.reset_password user@example.com
```

The password is entered through a hidden prompt and every existing session is revoked. The application does not claim email verification or offer an email reset flow; possession of an email-bound invitation is the onboarding proof.

## Storage and recovery

Neon stores accounts, teams, sessions, invitations, upload/extraction metadata and private R2 object keys. New file bytes are not stored in PostgreSQL. R2 holds:

- `raw_files/<team>/<extraction>/original.pdf`
- `processed_files/<team>/<extraction>/<artifact-id>`
- Temporary 8 MiB upload chunks under `raw_files/<team>/<upload>/parts/`.

File names stay in database metadata. Object keys use opaque IDs and team prefixes. Downloads check current membership before streaming the private object. Interrupted uploads expire after one day and are cleaned on subsequent uploads. A server restart marks unfinished extraction records as interrupted; originals remain available to download and retry. Background jobs continue after browser closure or logout, but do not automatically restart after a machine crash.

For a previous database version that contains binary artifacts:

```powershell
.\.venv\Scripts\python.exe -m scripts.migrate_storage
```

Each original is retained in PostgreSQL until its R2 upload succeeds and the object reference is committed. The command can be rerun. Normal migrations are additive and tracked in `schema_migrations`.

Install PostgreSQL client tools and run from the backend directory:

```powershell
.\.venv\Scripts\python.exe -m scripts.backup 'D:\AutusBackups'
```

This creates a custom-format metadata/database backup without exposing the database password in process arguments. Restrict the backup directory's Windows ACL to administrators and the designated backup account. Store another encrypted copy away from the server. Back up R2 objects independently: a database dump contains references, not file contents. Recovery requires both database metadata and matching R2 object keys. Test `pg_restore --no-owner --no-acl` into a separate database before replacing production. Neon recovery retention depends on the account's configured plan and should not be treated as the only backup.

## Verification and updates

```powershell
Get-Service AutusBackend,AutusFrontend,AutusTunnel
Invoke-RestMethod http://127.0.0.1:3000/api/health
Get-Content C:\ProgramData\Autus\active-release.txt
```

Check the public app in a private browser window: anonymous document access must fail. Sign in, process a PDF, reload history and download both original and generated output. Check membership removal and verify a different team cannot download the document. After installation, log out of Windows and check public health from another machine; repeat after a planned reboot. Test process recovery by terminating only the service's application child and confirming automatic restart.

For an update, run `deploy\update-all.bat` as administrator: it refuses to run with uncommitted changes to tracked files, fast-forwards the current branch, reinstalls with the tunnel exactly as configured today, and fails loudly if the backend does not answer `/health` afterwards. To do it by hand instead, pull/review the intended Git revision and run the installer again. Releases are kept in `C:\ProgramData\Autus\releases`. Private configuration stays in `config`; changing it requires updating the active backend `.env` and restarting the backend. Current service XML records the actual release paths. Save those XML files before an update. For rollback, stop the AUTUS services, restore the previous service XML and restart; review database compatibility first. Never restore an old unauthenticated release as a public production service.

Logs live in `C:\ProgramData\Autus\logs`. Diagnose the backend log first for connection/configuration failures, then frontend and tunnel logs. Never paste `.env`, tunnel credential files or signed object URLs into logs or support messages.

## Development checks

From backend, install `requirements-dev.txt`. Tests use a temporary schema in Neon, never production tables, and default to in-memory object storage. `AUTUS_R2_TEST=1` enables real R2 storage with an isolated prefix that is deleted afterward. `AUTUS_BROWSER_TEST=1` runs Chromium against the production frontend build; install Chromium using `playwright install chromium` first. Tests require ports 8000 and 3000 to be free.

- `python -m pytest -q`
- `AUTUS_R2_TEST=1 python -m pytest tests/test_documents.py tests/test_jobs.py -q`
- `AUTUS_BROWSER_TEST=1 python -m pytest tests/test_browser.py -q`
- Frontend: `npm ci`, `npm run lint`, `npm run build`, `npm audit`
- Backend: `pip-audit`

Reference: [Cloudflare Windows services](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/as-a-service/windows/), [WinSW configuration](https://github.com/winsw/winsw/blob/v2.12.0/doc/xmlConfigFile.md), [R2 S3 access](https://developers.cloudflare.com/r2/get-started/s3/), [OWASP password storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
