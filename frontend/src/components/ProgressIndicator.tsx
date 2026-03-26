"use client";

import { Progress } from "@/components/ui/progress";
import type { ExtractionStatus } from "@/lib/types";

interface ProgressIndicatorProps {
  status: ExtractionStatus;
  progress: number;
  stepLabel: string;
  resultUrl: string | null;
  csvUrl: string | null;
  rowCount: number | null;
}

export function ProgressIndicator({
  status,
  progress,
  stepLabel,
  resultUrl,
  csvUrl,
  rowCount,
}: ProgressIndicatorProps) {
  if (status === "idle") return null;

  const isLoading = status === "uploading" || status === "processing";
  const isDone = status === "done";

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        {isLoading && (
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
        )}
        <span className="text-sm text-gray-600">{stepLabel}</span>
      </div>

      <Progress value={progress} className="h-2" />

      {isDone && resultUrl && (
        <div className="flex flex-col gap-2 pt-2">
          <a
            href={resultUrl}
            download="timesheet.xlsx"
            className="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-blue-600 px-6 py-3 text-sm font-medium text-white transition-colors hover:bg-blue-700"
          >
            Baixar Timesheet (Excel)
          </a>
          {csvUrl && (
            <a
              href={csvUrl}
              download="pjecalc.csv"
              className="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-green-600 px-6 py-3 text-sm font-medium text-white transition-colors hover:bg-green-700"
            >
              Baixar PJeCalc (CSV)
            </a>
          )}
        </div>
      )}
    </div>
  );
}
