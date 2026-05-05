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
import type { ExtractionHook, ExtractionMode, ExtractionState } from "@/lib/types";

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
        excelFilename: null,
        csvUrl: null,
        csvExt: "csv",
        rowCount: null,
        provider: null,
        error: null,
      });

      setState((s) => ({ ...s, status: "processing" }));

      let interval: ReturnType<typeof setInterval> | undefined;

      if (
        mode !== "guia" &&
        mode !== "contracheque" &&
        mode !== "horas_extras" &&
        mode !== "frequencia"
      ) {
        const stages: Array<[number, string]> = [
          [10, "Enviando arquivo..."],
          [20, "Arquivo recebido. Analisando PDF..."],
          [40, "Extraindo registros..."],
          [60, "Processando dados..."],
          [75, "Gerando arquivos..."],
        ];
        let stageIndex = 0;
        interval = setInterval(() => {
          if (stageIndex < stages.length) {
            const [progress, label] = stages[stageIndex];
            setProgress(progress, label);
            stageIndex++;
          } else {
            clearInterval(interval);
          }
        }, 600);
      } else {
        setProgress(10, "Enviando arquivo...");
      }

      try {
        const handleChunkProgress = (chunk: number, total: number, message?: string) => {
          const pct = Math.round((chunk / total) * 80) + 10;
          setProgress(pct, message ?? `Processando parte ${chunk} de ${total}...`);
        };

        if (mode === "contracheque" || mode === "horas_extras") {
          const result = await (
            mode === "horas_extras"
              ? extractContrachequeExtraHours(file, handleChunkProgress)
              : extractContracheque(file, handleChunkProgress)
          );
          clearInterval(interval);

          setProgress(95, "Quase pronto...");
          await new Promise((r) => setTimeout(r, 300));

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
          clearInterval(interval);

          setProgress(95, "Quase pronto...");
          await new Promise((r) => setTimeout(r, 300));

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
              : extractTimesheet(file)
          );
          clearInterval(interval);

          setProgress(95, "Quase pronto...");
          await new Promise((r) => setTimeout(r, 300));

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
        if (interval) clearInterval(interval);
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
