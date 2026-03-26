export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface ExtractResult {
  blob: Blob;
  provider: string;
  rowCount: number;
}

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function extractTimesheet(file: File): Promise<ExtractResult> {
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
    let message = "Erro ao processar o PDF.";
    try {
      const body = await response.json();
      if (body?.error) message = body.error;
      else if (body?.detail) message = body.detail;
    } catch {
      // ignore JSON parse errors
    }
    throw new ApiError(message, response.status);
  }

  const blob = await response.blob();
  const provider = response.headers.get("x-provider-used") ?? "desconhecido";
  const rowCount = parseInt(response.headers.get("x-rows-extracted") ?? "0", 10);

  return { blob, provider, rowCount };
}
