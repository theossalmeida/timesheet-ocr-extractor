export type OcorrenciaTipo =
  | "ferias"
  | "feriado"
  | "falta_justificada"
  | "falta_injustificada"
  | "licenca_medica"
  | "afastamento"
  | "folga"
  | "dsr"
  | "trabalho_normal"
  | "meio_periodo"
  | "outro";

export interface TimesheetRow {
  data: string | null;
  entrada_1: string | null;
  saida_1: string | null;
  entrada_2: string | null;
  saida_2: string | null;
  ocorrencia_raw: string | null;
  ocorrencia_tipo: OcorrenciaTipo | null;
}

export interface ExtractionResult {
  rows: TimesheetRow[];
  provider: "pdfplumber" | "gemini" | "mistral" | "pdfplumber+gemini" | "pdfplumber+mistral";
  pdf_type: "native" | "scanned" | "mixed";
  warnings: string[];
  total_rows: number;
}

export type ExtractionMode = "cartao" | "guia" | "contracheque";

export type ExtractionStatus =
  | "idle"
  | "uploading"
  | "processing"
  | "done"
  | "error";

export interface ExtractionState {
  status: ExtractionStatus;
  progress: number;
  stepLabel: string;
  resultUrl: string | null;
  excelFilename: string | null;
  csvUrl: string | null;
  csvExt: string;
  rowCount: number | null;
  provider: string | null;
  error: string | null;
}

export interface ExtractionHook extends ExtractionState {
  upload: (file: File, mode: ExtractionMode) => Promise<void>;
  reset: () => void;
}
