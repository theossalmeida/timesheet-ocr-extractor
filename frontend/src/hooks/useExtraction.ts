"use client";

import { useCallback, useRef, useState } from "react";
import {
  extractTimesheet,
  extractGuia,
  extractContracheque,
  extractContrachequeExtraHours,
  extractFrequencia,
  ApiError,
} from "@/lib/api";
import type { ExtractionHook, ExtractionMode, ExtractionState, ProgressUpdate } from "@/lib/types";
import { PHASE_LABELS, UPLOADING_LABEL, phasePercent } from "@/lib/progress";

const IDLE_STATE: ExtractionState = {
  status: "idle",
  progress: 0,
  stepLabel: "",
  resultUrl: null,
  excelFilename: null,
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

  const upload = useCallback(
    async (file: File, mode: ExtractionMode) => {
      setState({
        status: "uploading",
        progress: 0,
        stepLabel: UPLOADING_LABEL,
        resultUrl: null,
        excelFilename: null,
        csvUrl: null,
        csvExt: "csv",
        rowCount: null,
        provider: null,
        error: null,
      });

      setState((s) => ({ ...s, status: "processing" }));

      try {
        // The bar only moves forward: `chunk` counts finished units, and those
        // arrive out of order once chunks run concurrently.
        const handleChunkProgress = (update: ProgressUpdate) => {
          const percent = phasePercent(update);
          if (percent === null) return;
          setState((s) => ({
            ...s,
            progress: Math.max(s.progress, percent),
            stepLabel: PHASE_LABELS[update.phase!],
          }));
        };

        if (mode === "contracheque" || mode === "horas_extras") {
          const result = await (
            mode === "horas_extras"
              ? extractContrachequeExtraHours(file, handleChunkProgress)
              : extractContracheque(file, handleChunkProgress)
          );
          const excelUrl = URL.createObjectURL(result.excelBlob);
          resultUrlRef.current = excelUrl;

          const columnsExtracted =
            "columnsExtracted" in result ? result.columnsExtracted : null;

          setState({
            status: "done",
            progress: 100,
            stepLabel:
              columnsExtracted === null
                ? `${result.monthsExtracted} ${result.monthsExtracted === 1 ? "mes processado" : "meses processados"}!`
                : `${result.monthsExtracted} meses e ${columnsExtracted} colunas processados!`,
            resultUrl: excelUrl,
            excelFilename: result.excelFilename,
            csvUrl: null,
            csvExt: "csv",
            rowCount: result.monthsExtracted,
            provider: result.provider,
            error: null,
          });
        } else if (mode === "frequencia") {
          const result = await extractFrequencia(file, handleChunkProgress);
          const excelUrl = URL.createObjectURL(result.excelBlob);
          resultUrlRef.current = excelUrl;

          setState({
            status: "done",
            progress: 100,
            stepLabel: `${result.rowCount} dias classificados!`,
            resultUrl: excelUrl,
            excelFilename: result.excelFilename,
            csvUrl: null,
            csvExt: "csv",
            rowCount: result.rowCount,
            provider: result.provider,
            error: null,
          });
        } else {
          const result = await (
            mode === "guia"
              ? extractGuia(file, handleChunkProgress)
              : extractTimesheet(file, handleChunkProgress)
          );
          const excelUrl = URL.createObjectURL(result.excelBlob);
          resultUrlRef.current = excelUrl;

          const csvUrl = URL.createObjectURL(result.csvBlob);
          csvUrlRef.current = csvUrl;

          setState({
            status: "done",
            progress: 100,
            stepLabel: `${result.rowCount} registros foram extraidos!`,
            resultUrl: excelUrl,
            excelFilename: result.excelFilename,
            csvUrl,
            csvExt: result.csvExt,
            rowCount: result.rowCount,
            provider: result.provider,
            error: null,
          });
        }
      } catch (err) {
        const message =
          err instanceof ApiError
            ? err.message
            : "Ocorreu um erro inesperado. Tente novamente.";
        setState({
          status: "error",
          progress: 0,
          stepLabel: "",
          resultUrl: null,
          excelFilename: null,
          csvUrl: null,
          csvExt: "csv",
          rowCount: null,
          provider: null,
          error: message,
        });
      }
    },
    [],
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
