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

export interface ContrachequeBundleResult {
  excelBlob: Blob;
  excelFilename: string;
  monthsExtracted: number;
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

export async function extractGuia(
  file: File,
  onProgress?: (chunk: number, total: number) => void,
): Promise<BundleResult> {
  const form = new FormData();
  form.append("file", file);

  let response: Response;
  try {
    response = await fetch(`${API_URL}/extract/guia`, {
      method: "POST",
      body: form,
      // No client-side timeout — backend streams keep-alives to prevent proxy timeouts
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

  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop()!;

    for (const part of parts) {
      const trimmed = part.trim();
      if (!trimmed || trimmed.startsWith(":")) continue; // keep-alive comment

      const dataLine = trimmed.split("\n").find((l) => l.startsWith("data: "));
      if (!dataLine) continue;

      let event: Record<string, unknown>;
      try {
        event = JSON.parse(dataLine.slice(6));
      } catch {
        continue;
      }

      if (event.type === "progress") {
        onProgress?.(event.chunk as number, event.total as number);
      } else if (event.type === "done") {
        return {
          excelBlob: b64ToBlob(
            event.excel_b64 as string,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          ),
          excelFilename: event.excel_filename as string,
          csvBlob: b64ToBlob(event.csv_b64 as string, event.csv_mime as string),
          csvFilename: event.csv_filename as string,
          csvExt: (event.csv_mime as string)?.includes("zip") ? "zip" : "csv",
          rowCount: (event.rows_extracted as number) ?? 0,
          provider: (event.provider as string) ?? "gemini-guia",
        };
      } else if (event.type === "error") {
        throw new ApiError((event.message as string) ?? "Erro ao processar guias.", 422);
      }
    }
  }

  throw new ApiError("Processamento interrompido inesperadamente.", 500);
}

export async function extractContracheque(
  file: File,
  onProgress?: (chunk: number, total: number, message?: string) => void,
): Promise<ContrachequeBundleResult> {
  const form = new FormData();
  form.append("file", file);

  let response: Response;
  try {
    response = await fetch(`${API_URL}/contracheque`, {
      method: "POST",
      body: form,
      // No client-side timeout — backend streams keep-alives to prevent proxy timeouts
    });
  } catch {
    throw new ApiError("Não foi possível conectar ao servidor.", 0);
  }

  if (!response.ok) {
    throw new ApiError(
      await _parseError(response, "Erro ao processar o contracheque."),
      response.status,
    );
  }

  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop()!;

    for (const part of parts) {
      const trimmed = part.trim();
      if (!trimmed || trimmed.startsWith(":")) continue; // keep-alive comment

      const dataLine = trimmed.split("\n").find((l) => l.startsWith("data: "));
      if (!dataLine) continue;

      let event: Record<string, unknown>;
      try {
        event = JSON.parse(dataLine.slice(6));
      } catch {
        continue;
      }

      if (event.type === "progress") {
        onProgress?.(event.chunk as number, event.total as number, event.message as string | undefined);
      } else if (event.type === "done") {
        return {
          excelBlob: b64ToBlob(
            event.excel_b64 as string,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          ),
          excelFilename: event.excel_filename as string,
          monthsExtracted: (event.months_extracted as number) ?? 0,
          provider: (event.provider as string) ?? "pdfplumber",
        };
      } else if (event.type === "error") {
        throw new ApiError((event.message as string) ?? "Erro ao processar contracheque.", 422);
      }
    }
  }

  throw new ApiError("Processamento interrompido inesperadamente.", 500);
}
