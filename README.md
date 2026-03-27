# Timesheet Extractor

Extrai registros de ponto de PDFs trabalhistas brasileiros e gera Excel + CSV (PJeCalc) prontos para uso.

Suporta dois modos:

- **Cartão de Ponto** — PDFs de folha de ponto (nativos ou escaneados)
- **Guia Ministerial** — Papeletas de serviço externo (ex: motoristas)

## Como funciona

```
PDF → pdfplumber → Gemini 3 Flash (fallback / OCR)
                                    ↓
                          Excel estilizado + CSV PJeCalc
```

O backend tenta extração nativa via `pdfplumber` primeiro. Se o PDF for escaneado ou a extração falhar, usa Gemini 3 Flash. Guia ministerial usa Gemini 3 Flash diretamente por chunks com progresso em tempo real via SSE.

## Stack

| Camada | Tecnologia |
|---|---|
| Backend | FastAPI + Python 3.12 |
| Extração | pdfplumber, Google Gemini 3 Flash |
| Excel | openpyxl |
| Frontend | Next.js 14 + TypeScript + Tailwind |
| Deploy | Fly.io (região `gru` — São Paulo) |

## Rodando localmente

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env  # edite com suas chaves
uvicorn main:app --reload --port 8000
```

`.env` necessário:

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

Acesse `http://localhost:3000`.

## Endpoints

| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/health` | Health check |
| `POST` | `/extract` | Extrai cartão de ponto → JSON com Excel + CSV em base64 |
| `POST` | `/extract/guia` | Extrai guia ministerial → SSE com progresso + resultado final |
| `POST` | `/preview` | Extrai sem gerar arquivos (debug) |

Rate limit: 10 req/min por IP.

## Deploy (Fly.io)

Dois apps na org Personal, região São Paulo:

```
timesheet-api             → backend  (1GB RAM)
timesheet-app-damp-forest-8112 → frontend (512MB RAM)
```

Ambos suspendem automaticamente quando ociosos (`min_machines_running = 0`).

### Backend

```bash
cd backend
fly secrets set \
  GEMINI_API_KEY="..." \
  MISTRAL_API_KEY="..." \
  CORS_ORIGINS='["https://timesheet-app-damp-forest-8112.fly.dev"]'
fly deploy
```

### Frontend

`NEXT_PUBLIC_API_URL` já está configurada como build arg no `fly.toml` — sem secrets adicionais.

```bash
cd frontend
fly deploy
```

## Formatos de PDF suportados

- Tabela nativa com colunas de entrada/saída
- Multirow com células mescladas (DD/mmm/YY)
- Texto fixo — formato "FOLHA DE PONTO"
- PDFs escaneados (via Gemini OCR)
- PDFs mistos (páginas nativas + escaneadas)

## Saída

**Excel** — duas abas:
- *Registros de Ponto*: linhas com data, entradas/saídas, ocorrência (cores por tipo)
- *Resumo*: total de registros, intervalo de datas, contagem por tipo de ocorrência

**CSV** — formato PJeCalc (`;` delimitado, UTF-8 BOM), com todos os dias do calendário preenchidos.
