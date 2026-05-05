# Timesheet Extractor

Extracts Brazilian labor PDFs and generates Excel/CSV files ready for use.

Supported modes:

- **Cartao de Ponto** - standard timecard PDFs, native or scanned
- **Guia Ministerial** - external service logs, such as drivers' ministerial guides
- **Contracheque** - Petrobras paycheck PDFs into yearly salary sheets
- **Horas Extras** - Petrobras paycheck PDFs into a month-by-month Excel with one dynamic column per extra-hour item

## How It Works

```text
PDF -> pdfplumber -> Gemini 3 Flash when native extraction fails
                         |
                         v
                 Styled Excel / PJeCalc CSV
```

The backend first attempts native extraction with `pdfplumber`. If a page cannot be parsed, the paycheck and extra-hours flows send only the failed pages to Gemini. Ministerial guides are processed by Gemini in chunks, with real-time progress streamed through SSE.

## Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI + Python 3.12 |
| Extraction | pdfplumber, Google Gemini 3 Flash |
| Excel | openpyxl |
| Frontend | Next.js 14 + TypeScript + Tailwind |
| Deploy | Fly.io, region `gru` |

## Running Locally

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
| `POST` | `/extract` | Extract timecard to JSON with Excel + CSV as base64 |
| `POST` | `/extract/guia` | Extract ministerial guide to an SSE stream with progress and final result |
| `POST` | `/contracheque` | Extract Petrobras paychecks to a salary Excel through SSE |
| `POST` | `/contracheque/horas-extras` | Extract only extra-hour paycheck items to a dynamic Excel through SSE |
| `POST` | `/preview` | Extract without generating files, for debugging |

Rate limit: 10 req/min per IP.

## Deploy

Two Fly.io apps under the Personal org, Sao Paulo region:

```text
timesheet-api                  -> backend  (1 GB RAM)
timesheet-app-damp-forest-8112 -> frontend (512 MB RAM)
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

`NEXT_PUBLIC_API_URL` is already set as a build arg in `fly.toml`, so no additional secrets are needed.

```bash
cd frontend
fly deploy
```

## Supported PDF Formats

- Native table with entry/exit columns
- Multirow with merged cells, such as `DD/mmm/YY`
- Fixed-width text in `FOLHA DE PONTO` format
- Petrobras paycheck PDFs
- Scanned PDFs through Gemini 3 Flash OCR
- Hybrid PDFs mixing native and scanned pages

## Output

**Cartao de Ponto Excel** - two sheets:

- *Timesheet Records*: rows with date, entry/exit times, and occurrence type
- *Summary*: total records, date range, and occurrence type counts

**PJeCalc CSV** - `;` delimited, UTF-8 BOM, with every calendar day filled in.

**Contracheque Excel** - salary sheets organized by year and month.

**Horas Extras Excel** - one row per month, one dynamic column per extra-hour item, and a final total column.
