"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Upload, Search, Download, Check } from "lucide-react";
import { useDropzone } from "react-dropzone";
import { api, download, extract, type Artifact, type History, type Team } from "@/lib/client";
import { Blueprint } from "./Blueprint";

const modes = [
  { key: "cartao", label: "Cartão de Ponto", hint: "Timecard nativo ou digitalizado", max: 50, path: "/extract/stream", description: "Envie o PDF de cartão de ponto e baixe a planilha Excel formatada, com o CSV do PJeCalc." },
  { key: "guia", label: "Guia Ministerial", hint: "Papeletas, uma aba por motorista", max: 200, path: "/extract/guia", description: "Envie o PDF com guias ministeriais ou papeletas e baixe o Excel com uma aba por motorista." },
  { key: "contracheque", label: "Contracheque", hint: "Ficha salarial por ano e mês", max: 200, path: "/contracheque", description: "Envie o PDF de contracheques da Petrobras e baixe a ficha salarial em Excel organizada por ano e mês." },
  { key: "horas_extras", label: "Horas Extras", hint: "Uma coluna por verba", max: 200, path: "/contracheque/horas-extras", description: "Envie o PDF de contracheques da Petrobras e baixe uma planilha mensal apenas com verbas de horas extras." },
  { key: "frequencia", label: "Frequência", hint: "Classificação diária de ciclos", max: 200, path: "/extract/frequencia", description: "Envie o relatório de frequência da Petrobras e baixe a classificação diária de ciclos." },
];
const brl = (value: number) => value.toLocaleString("pt-BR", { style: "currency", currency: "BRL", maximumFractionDigits: 3 });
const statusLabels: Record<string, string> = { done: "Concluído", failed: "Falhou", processing: "Processando", interrupted: "Interrompido" };

export function Extractor({ team, onBusy }: { team: Team; onBusy: (busy: boolean) => void }) {
  const [mode, setMode] = useState(modes[0]);
  const [status, setStatus] = useState("idle");
  const [progress, setProgress] = useState(0);
  const [step, setStep] = useState("");
  const [filename, setFilename] = useState("");
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [history, setHistory] = useState<History | null>(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [downloadId, setDownloadId] = useState("");
  const [loading, setLoading] = useState(false);
  const generation = useRef(0);
  const busy = status === "running";

  const refresh = useCallback(async () => {
    const current = ++generation.current;
    const result = await api<History>(`/documents?q=${encodeURIComponent(query)}`, "GET", undefined, team.id);
    if (current === generation.current) setHistory(result);
  }, [team.id, query]);

  useEffect(() => {
    const timer = setTimeout(() => { refresh().catch(error => setError(error.message)); }, 250);
    return () => { clearTimeout(timer); generation.current++; };
  }, [refresh]);
  useEffect(() => {
    const timer = setInterval(() => { refresh().catch(error => setError(error.message)); }, 10000);
    return () => clearInterval(timer);
  }, [refresh]);
  useEffect(() => {
    if (!busy) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [busy]);

  async function upload(file: File) {
    if (busy) return;
    setError(""); setFilename(file.name); setStatus("running"); setProgress(0); setStep("Enviando arquivo…"); onBusy(true);
    try { setArtifacts(await extract(file, mode.key, team.id, (message, value) => { setStep(message); setProgress(value); })); setStatus("done"); }
    catch (error) { setStatus("error"); setError(error instanceof Error ? error.message : "Não foi possível processar o documento."); }
    finally { onBusy(false); await refresh().catch(error => setError(error.message)); }
  }

  const { getRootProps, getInputProps, isDragActive } = useDropzone({ accept: { "application/pdf": [".pdf"] }, maxSize: mode.max * 1024 * 1024, multiple: false, disabled: busy, onDropAccepted: files => void upload(files[0]), onDropRejected: () => setError(`Selecione um único PDF de até ${mode.max} MB.`) });

  async function save(artifact: Artifact) {
    setDownloadId(artifact.id); setError("");
    try { await download(artifact, team.id); }
    catch (error) { setError(error instanceof Error ? error.message : "Não foi possível baixar o arquivo."); }
    finally { setDownloadId(""); }
  }

  async function more() {
    if (!history) return;
    const current = generation.current;
    setLoading(true);
    try { const next = await api<History>(`/documents?q=${encodeURIComponent(query)}&offset=${history.documents.length}`, "GET", undefined, team.id); if (current === generation.current) setHistory({ ...next, documents: [...history.documents, ...next.documents] }); }
    catch (error) { setError(error instanceof Error ? error.message : "Não foi possível carregar o histórico."); }
    finally { setLoading(false); }
  }

  return <div className="workspace"><main className="extraction-main"><section><h6>1 · Tipo de documento</h6><div className="modes">{modes.map(item => <button key={item.key} type="button" className={`mode ${item.key === mode.key ? "selected" : ""}`} disabled={busy} aria-pressed={item.key === mode.key} onClick={() => { setMode(item); setStatus("idle"); setError(""); }}><span>{item.label}</span><small>{item.hint}</small></button>)}</div></section><section><h6>2 · Arquivo</h6><p className="text-muted description">{mode.description}</p>
    {error && <p role="alert" className="error">{error}</p>}
    {(status === "idle" || status === "error") && <Blueprint><div {...getRootProps({ className: `upload ${isDragActive ? "dragging" : ""}`, "aria-label": "Selecionar PDF para extração" })}><input {...getInputProps()} /><Upload size={28} strokeWidth={1.5} /><strong>{isDragActive ? "Solte o PDF aqui" : "Arraste o PDF aqui ou clique para selecionar"}</strong><span className="text-muted small">Apenas PDF · máx. {mode.max} MB</span></div></Blueprint>}
    {busy && <Blueprint className="panel stack"><div className="row"><h4>{step}</h4><span className="text-muted small push">{progress}%</span></div><div role="progressbar" aria-label="Processamento do PDF" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress} className="progress-track"><div style={{ width: `${progress}%` }} /></div><p className="small text-muted" role="status">{filename}</p></Blueprint>}
    {status === "done" && <Blueprint className="panel stack"><div className="row"><Check size={22} strokeWidth={1.5} /><div><h4>Planilha gerada</h4><p className="small text-muted">{filename} · arquivos salvos no histórico da equipe</p></div></div><div className="row wrap">{artifacts.map(artifact => <button className={`btn ${artifact.kind === "excel" ? "btn-primary" : "btn-secondary"}`} key={artifact.id} disabled={Boolean(downloadId)} onClick={() => void save(artifact)}><Download size={15} strokeWidth={1.5} />{downloadId === artifact.id ? "Baixando…" : artifact.kind === "excel" ? "Baixar Excel" : "Baixar PJeCalc (CSV / ZIP)"}</button>)}<button className="btn btn-ghost" onClick={() => { setStatus("idle"); setArtifacts([]); }}>Processar outro PDF</button></div></Blueprint>}
    </section></main><aside className="history"><Blueprint className="panel stack compact"><span className="card-kicker">Custo de IA · {new Date().toLocaleDateString("pt-BR", { month: "long" })}</span><span className="monthly-total">{history ? brl(history.summary.known_cost_brl) : "—"}</span><span className="small text-muted">{history?.summary.documents ?? 0} documentos neste mês</span><span className="small text-muted">{history?.summary.unknown_costs ? `${history.summary.unknown_costs} documento(s) sem custo informado. Total parcial.` : Number(history?.summary.known_cost_brl ?? 0) > 0 ? "Custo de IA medido por chamada." : "Processamento local sem cobrança por uso de IA."}</span></Blueprint><div><h6>Histórico da equipe</h6><div className="search"><Search size={15} strokeWidth={1.5} /><input type="search" className="input" aria-label="Buscar arquivo ou tipo" placeholder="Buscar arquivo ou tipo" value={query} onChange={event => setQuery(event.target.value)} /></div></div><div>{history === null && <p className="text-muted small" role="status">Carregando histórico…</p>}{history?.documents.length === 0 && <p className="text-muted small">{query ? "Nenhum documento corresponde à busca." : "Os documentos processados pela equipe aparecerão aqui."}</p>}{history?.documents.map(document => <article key={document.id} className="history-item"><div className="row"><span className="tag tag-accent">{modes.find(item => item.key === document.mode)?.label ?? document.mode}</span><span className="small text-muted push">{document.cost_brl === null ? "custo não informado" : Number(document.cost_brl) === 0 ? "sem custo de IA" : brl(Number(document.cost_brl))}</span></div><div className="filename" title={document.filename}>{document.filename}</div><div className="row wrap small"><span className="text-muted">{new Date(document.created_at).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })} · {document.user_name}</span><span>{statusLabels[document.status] ?? document.status}</span></div><div className="row wrap">{document.artifacts.map(artifact => <button key={artifact.id} className="btn btn-ghost small" disabled={Boolean(downloadId)} onClick={() => void save(artifact)}><Download size={13} strokeWidth={1.5} />{downloadId === artifact.id ? "Baixando…" : artifact.kind === "original" ? "PDF original" : artifact.kind === "excel" ? "Excel" : "CSV / ZIP"}</button>)}</div></article>)}</div>{history?.has_more && <button className="btn btn-secondary" disabled={loading} onClick={() => void more()}>{loading ? "Carregando…" : "Carregar mais"}</button>}</aside></div>;
}
