export interface Team { id: string; name: string; role: "admin" | "member" }
export interface Account { id: string; name: string; email: string }
export interface Artifact { id: string; kind: "original" | "excel" | "csv"; filename: string }
export interface DocumentEntry { id: string; filename: string; mode: string; status: string; provider: string | null; row_count: number | null; cost_brl: number | null; created_at: string; user_name: string; artifacts: Artifact[] }
export interface History { documents: DocumentEntry[]; has_more: boolean; summary: { documents: number; known_cost_brl: number; unknown_costs: number } }

export class RequestError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message);
  }
}

export async function request(path: string, options: RequestInit = {}, teamId?: string): Promise<Response> {
  const headers = new Headers(options.headers);
  headers.set("x-autus-request", "1");
  if (teamId) headers.set("x-team-id", teamId);
  const response = await fetch(`/api${path}`, { ...options, headers, credentials: "same-origin", cache: "no-store" });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const message = typeof body?.error === "string" ? body.error : typeof body?.detail === "string" ? body.detail : response.status === 422 ? "Confira os campos preenchidos." : "Não foi possível concluir a solicitação.";
    if (response.status === 401) window.dispatchEvent(new Event("autus:unauthorized"));
    throw new RequestError(message, response.status);
  }
  return response;
}

export async function retryUploadRequest(path: string, options: RequestInit, teamId: string): Promise<Response> {
  for (let attempt = 0; ; attempt++) {
    try {
      return await request(path, options, teamId);
    } catch (error) {
      const retryable = error instanceof TypeError || (error instanceof RequestError && (error.status === 408 || error.status === 429 || error.status >= 500));
      if (!retryable || attempt >= 2) throw error;
      await new Promise(resolve => setTimeout(resolve, 1000 * 2 ** attempt));
    }
  }
}

export async function api<T>(path: string, method = "GET", data?: unknown, teamId?: string): Promise<T> {
  return (await request(path, { method, ...(data !== undefined ? { headers: { "content-type": "application/json" }, body: JSON.stringify(data) } : {}) }, teamId)).json();
}

export async function download(artifact: Artifact, teamId: string) {
  const response = await request(`/documents/files/${artifact.id}`, {}, teamId);
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = artifact.filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

export async function extract(file: File, mode: string, teamId: string, onProgress: (message: string, progress: number) => void): Promise<Artifact[]> {
  const upload = await api<{ id: string; chunk_size: number }>("/uploads", "POST", { filename: file.name, mode, size_bytes: file.size }, teamId);
  let jobId = "";
  try {
    for (let offset = 0, part = 0; offset < file.size; offset += upload.chunk_size, part++) {
      await retryUploadRequest(`/uploads/${upload.id}/${part}`, { method: "PUT", body: file.slice(offset, offset + upload.chunk_size), headers: { "content-type": "application/octet-stream" } }, teamId);
      onProgress("Enviando arquivo…", Math.round(Math.min(offset + upload.chunk_size, file.size) / file.size * 15));
    }
    const job: { id: string } = await (await retryUploadRequest(`/uploads/${upload.id}/process`, { method: "POST" }, teamId)).json();
    jobId = job.id;
  } catch (error) {
    await api(`/uploads/${upload.id}`, "DELETE", undefined, teamId).catch(() => undefined);
    throw error;
  }
  let failures = 0;
  while (true) {
    await new Promise(resolve => setTimeout(resolve, 2000));
    let result: { status: string; error: string; artifacts: Artifact[]; progress: { message?: string; chunk?: number; total?: number } };
    try { result = await api(`/documents/${jobId}`, "GET", undefined, teamId); failures = 0; }
    catch (error) { if (++failures >= 5) throw error; onProgress("Reconectando… Seu documento continua no histórico.", 15); continue; }
    if (result.status === "done") return result.artifacts;
    if (result.status !== "processing") throw new Error(result.error ?? "Processamento interrompido. Confira o histórico.");
    const value = result.progress;
    onProgress(value.message ?? "Processando documento…", value.total ? 15 + Math.min(80, Math.round((value.chunk ?? 0) / value.total * 80)) : 15);
  }
}
