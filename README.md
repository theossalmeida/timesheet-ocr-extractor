# Timesheet Extractor

Extracts timesheet records from Brazilian labor PDFs and generates Excel + CSV (PJeCalc) files ready for use.

Supports two modes:

- **Cartão de Ponto** — standard timecard PDFs (native or scanned)
- **Guia Ministerial** — external service logs (e.g. drivers' ministerial guides)

## How it works

```
PDF → pdfplumber → Gemini 3 Flash (fallback / OCR)
                                    ↓
                         Styled Excel + PJeCalc CSV
```

The backend first attempts native extraction via `pdfplumber`. If the PDF is scanned or extraction fails, it falls back to Gemini 3 Flash. Ministerial guides are processed directly by Gemini in chunks, with real-time progress streamed via SSE.

## Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI + Python 3.12 |
| Extraction | pdfplumber, Google Gemini 3 Flash |
| Excel | openpyxl |
| Frontend | Next.js 14 + TypeScript + Tailwind |
| Deploy | Fly.io (region `gru` — São Paulo) |

## Running locally

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env  # fill in your keys
uvicorn main:app --reload --port 8000
```

Required `.env`:

```env
GEMINI_API_KEY=...
CORS_ORIGINS=["http://localhost:3000"]
```

### Frontend

```bash
cd frontend
npm install

echo "NEXT_PUBLIC_API_URL=http://localhost:8000" > .env.local
npm run dev
```

Open `http://localhost:3000`.

## Endpoints

| Method | Route | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `POST` | `/extract` | Extract timecard → JSON with Excel + CSV as base64 |
| `POST` | `/extract/guia` | Extract ministerial guide → SSE stream with progress + final result |
| `POST` | `/preview` | Extract without generating files (debug) |

Rate limit: 10 req/min per IP.

## Deploy (Fly.io)

Two apps under the Personal org, São Paulo region:

```
timesheet-api                  → backend  (1 GB RAM)
timesheet-app-damp-forest-8112 → frontend (512 MB RAM)
```

Both suspend automatically when idle (`min_machines_running = 0`).

### Backend

```bash
cd backend
fly secrets set \
  GEMINI_API_KEY="..." \
  CORS_ORIGINS='["https://timesheet-app-damp-forest-8112.fly.dev"]'
fly deploy
```

### Frontend

`NEXT_PUBLIC_API_URL` is already set as a build arg in `fly.toml` — no additional secrets needed.

```bash
cd frontend
fly deploy
```

## Supported PDF formats

- Native table with entry/exit columns
- Multirow with merged cells (DD/mmm/YY)
- Fixed-width text — "FOLHA DE PONTO" format
- Scanned PDFs (via Gemini 3 Flash OCR)
- Hybrid PDFs (mix of native and scanned pages)

## Output

**Excel** — two sheets:
- *Timesheet Records*: rows with date, entry/exit times, occurrence type (color-coded)
- *Summary*: total records, date range, count by occurrence type

**CSV** — PJeCalc format (`;` delimited, UTF-8 BOM), with every calendar day filled in.
