# AUTUS

Private team workspace for extracting Brazilian labor documents into Excel and PJeCalc CSV/ZIP files. Supports timecards, ministerial guides, Petrobras paychecks, extra hours and frequency cycles.

Production: https://timesheet.theosantoro.dev

- Next.js 16 / React 19 frontend following the supplied AUTUS design.
- FastAPI extraction using native PDF parsing, Tesseract, and configured AI fallbacks.
- Email/password authentication, revocable sessions, team membership and email-bound invitation links.
- Neon PostgreSQL for metadata; private Cloudflare R2 for originals and generated files.
- Per-call metering of paid AI usage: tokens are priced from litellm's model price map and converted to BRL for the team's monthly total.
- Chunked uploads and background processing; team history and authenticated downloads.
- Windows services for frontend, backend and the existing Cloudflare tunnel.

See [Windows installation and operations](deploy/README.md) for installation, first login, backups, recovery and verification. The implementation record is in [the v2 plan](docs/V2-PLAN.md).

For development, configure `backend/.env` using [the example](backend/.env.example), setting `APP_ORIGIN=http://localhost:3000` and `COOKIE_SECURE=false` only for local HTTP. Install `backend/requirements-dev.txt`, run `uvicorn main:app --host 127.0.0.1 --port 8000` from backend, then `npm ci` and `npm run dev` from frontend. The frontend uses its same-origin `/api` proxy; no public backend URL is embedded in the browser bundle.

The tests create and clean a separate PostgreSQL schema. Run `python -m pytest -q` from backend, and `npm run lint`, `npm run build`, and `npm audit` from frontend. Real R2 and Chromium checks are documented in the operations guide.
