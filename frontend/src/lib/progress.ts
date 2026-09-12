import type { ExtractionPhase, ProgressUpdate } from "./types";

/**
 * One progress vocabulary for every extraction mode.
 *
 * The backend reports which phase it is in and how many units it has finished;
 * this file is the only place that turns that into a percentage and a label, so
 * the bar reads the same whichever pipeline ran underneath.
 */

export const UPLOADING_LABEL = "Enviando arquivo...";

export const PHASE_LABELS: Record<ExtractionPhase, string> = {
  queued: "Aguardando processamento...",
  detecting: "Identificando melhor abordagem...",
  processing: "Processando arquivo...",
  building: "Montando planilha...",
};

// Each phase owns a slice of the bar. Only processing has units to count, so it
// gets nearly all of it; the rest hold their edge and only change the label.
export const PHASE_RANGES: Record<ExtractionPhase, [number, number]> = {
  queued: [0, 0],
  detecting: [0, 0],
  processing: [0, 99],
  building: [99, 100],
};

/** Percentage for a frame, or null when it carries no phase to place it in. */
export function phasePercent({ phase, chunk, total }: ProgressUpdate): number | null {
  if (!phase || !(phase in PHASE_RANGES)) return null;
  const [start, end] = PHASE_RANGES[phase];
  const units = typeof total === "number" && total > 0 ? total : 1;
  const finished = typeof chunk === "number" && Number.isFinite(chunk) ? chunk : 0;
  const ratio = Math.min(1, Math.max(0, finished / units));
  return Math.round(start + ratio * (end - start));
}

export function phaseLabel(phase: ExtractionPhase | undefined): string | null {
  return phase && phase in PHASE_LABELS ? PHASE_LABELS[phase] : null;
}
