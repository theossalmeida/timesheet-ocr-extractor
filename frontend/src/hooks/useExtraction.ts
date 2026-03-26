"use client";

import { useCallback, useRef, useState } from "react";
import { extractTimesheet, ApiError } from "@/lib/api";
import type { ExtractionHook, ExtractionState } from "@/lib/types";

const IDLE_STATE: ExtractionState = {
  status: "idle",
  progress: 0,
  stepLabel: "",
  resultUrl: null,
  rowCount: null,
  provider: null,
  error: null,
};

export function useExtraction(): ExtractionHook {
  const [state, setState] = useState<ExtractionState>(IDLE_STATE);
  const resultUrlRef = useRef<string | null>(null);

  const setProgress = useCallback((progress: number, stepLabel: string) => {
    setState((s) => ({ ...s, progress, stepLabel }));
  }, []);

  const upload = useCallback(
    async (file: File) => {
      setState({
        status: "uploading",
        progress: 0,
        stepLabel: "Enviando arquivo...",
        resultUrl: null,
        rowCount: null,
        provider: null,
        error: null,
      });

      // Simulated progress stages
      const stages: Array<[number, string, number]> = [
        [10, "Enviando arquivo...", 300],
        [20, "Arquivo recebido. Analisando PDF...", 500],
        [40, "Extraindo registros...", 800],
        [60, "Processando dados...", 600],
        [75, "Gerando planilha Excel...", 400],
      ];

      let stageIndex = 0;
      const interval = setInterval(() => {
        if (stageIndex < stages.length) {
          const [progress, label] = stages[stageIndex];
          setProgress(progress, label);
          stageIndex++;
        } else {
          clearInterval(interval);
        }
      }, 600);

      setState((s) => ({ ...s, status: "processing" }));

      try {
        const { blob, provider, rowCount } = await extractTimesheet(file);
        clearInterval(interval);

        setProgress(95, "Quase pronto...");
        await new Promise((r) => setTimeout(r, 300));

        const url = URL.createObjectURL(blob);
        resultUrlRef.current = url;

        setState({
          status: "done",
          progress: 100,
          stepLabel: `${rowCount} registros extraídos via ${provider}`,
          resultUrl: url,
          rowCount,
          provider,
          error: null,
        });
      } catch (err) {
        clearInterval(interval);
        const message =
          err instanceof ApiError
            ? err.message
            : "Ocorreu um erro inesperado. Tente novamente.";
        setState({
          status: "error",
          progress: 0,
          stepLabel: "",
          resultUrl: null,
          rowCount: null,
          provider: null,
          error: message,
        });
      }
    },
    [setProgress],
  );

  const reset = useCallback(() => {
    if (resultUrlRef.current) {
      URL.revokeObjectURL(resultUrlRef.current);
      resultUrlRef.current = null;
    }
    setState(IDLE_STATE);
  }, []);

  return { ...state, upload, reset };
}
