export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface BundleResult {
  excelBlob: Blob;
  excelFilename: string;
  csvBlob: Blob;
  csvFilename: string;
  csvExt: string;
  rowCount: number;
  provider: string;
}

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function b64ToBlob(b64: string, mimeType: string): Blob {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  return new Blob([bytes], { type: mimeType });
}

async function _parseError(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json();
    if (body?.error) return body.error;
    if (body?.detail) return body.detail;
  } catch {
    // ignore
  }
  return fallback;
}

export async function extractTimesheet(file: File): Promise<BundleResult> {
  const form = new FormData();
  form.append("file", file);

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 150_000);

  let response: Response;
  try {
    response = await fetch(`${API_URL}/extract`, {
      method: "POST",
      body: form,
      signal: controller.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new ApiError("A requisição excedeu o tempo limite (150s).", 408);
    }
    throw new ApiError("Não foi possível conectar ao servidor.", 0);
  } finally {
    clearTimeout(timeout);
  }

  if (!response.ok) {
    throw new ApiError(await _parseError(response, "Erro ao processar o PDF."), response.status);
  }

  const data = await response.json();
  return {
    excelBlob: b64ToBlob(data.excel_b64, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    excelFilename: data.excel_filename,
    csvBlob: b64ToBlob(data.csv_b64, data.csv_mime),
    csvFilename: data.csv_filename,
    csvExt: data.csv_mime?.includes("zip") ? "zip" : "csv",
    rowCount: data.rows_extracted ?? 0,
    provider: data.provider ?? "desconhecido",
  };
}

export async function extractGuia(file: File): Promise<BundleResult> {
  const form = new FormData();
  form.append("file", file);

  let response: Response;
  try {
    response = await fetch(`${API_URL}/extract/guia`, {
      method: "POST",
      body: form,
      // No timeout — guia processing can take several minutes
    });
  } catch {
    throw new ApiError("Não foi possível conectar ao servidor.", 0);
  }

  if (!response.ok) {
    throw new ApiError(
      await _parseError(response, "Erro ao processar as guias ministeriais."),
      response.status,
    );
  }

  const data = await response.json();
  return {
    excelBlob: b64ToBlob(data.excel_b64, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    excelFilename: data.excel_filename,
    csvBlob: b64ToBlob(data.csv_b64, data.csv_mime),
    csvFilename: data.csv_filename,
    csvExt: data.csv_mime?.includes("zip") ? "zip" : "csv",
    rowCount: data.rows_extracted ?? 0,
    provider: data.provider ?? "gemini-guia",
  };
}
