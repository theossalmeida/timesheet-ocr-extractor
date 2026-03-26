"use client";

import { useCallback, useRef, useState } from "react";
import {
  extractTimesheet,
  extractTimesheetCSV,
  extractGuia,
  extractGuiaCSV,
  ApiError,
} from "@/lib/api";
import type { ExtractionHook, ExtractionMode, ExtractionState } from "@/lib/types";

const IDLE_STATE: ExtractionState = {
  status: "idle",
  progress: 0,
  stepLabel: "",
  resultUrl: null,
  csvUrl: null,
  csvExt: "csv",
  rowCount: null,
  provider: null,
  error: null,
};

export function useExtraction(): ExtractionHook {
  const [state, setState] = useState<ExtractionState>(IDLE_STATE);
  const resultUrlRef = useRef<string | null>(null);
  const csvUrlRef = useRef<string | null>(null);

  const setProgress = useCallback((progress: number, stepLabel: string) => {
    setState((s) => ({ ...s, progress, stepLabel }));
  }, []);

  const upload = useCallback(
    async (file: File, mode: ExtractionMode) => {
      setState({
        status: "uploading",
        progress: 0,
        stepLabel: "Enviando arquivo...",
        resultUrl: null,
        csvUrl: null,
        csvExt: "csv",
        rowCount: null,
        provider: null,
        error: null,
      });

      const stages: Array<[number, string, number]> = [
        [10, "Enviando arquivo...", 300],
        [20, "Arquivo recebido. Analisando PDF...", 500],
        [40, "Extraindo registros...", 800],
        [60, "Processando dados...", 600],
        [75, "Gerando arquivos...", 400],
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
        let excelResult: Awaited<ReturnType<typeof extractTimesheet>>;
        let csvBlob: Blob;
        let csvExt = "csv";

        if (mode === "guia") {
          const [guiaResult, guiaCsv] = await Promise.all([
            extractGuia(file),
            extractGuiaCSV(file),
          ]);
          excelResult = guiaResult;
          csvBlob = guiaCsv.blob;
          csvExt = guiaCsv.ext;
        } else {
          const [timesheetResult, timesheetCsv] = await Promise.all([
            extractTimesheet(file),
            extractTimesheetCSV(file),
          ]);
          excelResult = timesheetResult;
          csvBlob = timesheetCsv;
        }

        clearInterval(interval);

        setProgress(95, "Quase pronto...");
        await new Promise((r) => setTimeout(r, 300));

        const url = URL.createObjectURL(excelResult.blob);
        resultUrlRef.current = url;

        const csvUrl = URL.createObjectURL(csvBlob);
        csvUrlRef.current = csvUrl;

        setState({
          status: "done",
          progress: 100,
          stepLabel: `${excelResult.rowCount} registros foram extraídos!`,
          resultUrl: url,
          csvUrl,
          csvExt,
          rowCount: excelResult.rowCount,
          provider: excelResult.provider,
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
          csvUrl: null,
          csvExt: "csv",
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
    if (csvUrlRef.current) {
      URL.revokeObjectURL(csvUrlRef.current);
      csvUrlRef.current = null;
    }
    setState(IDLE_STATE);
  }, []);

  return { ...state, upload, reset };
}
