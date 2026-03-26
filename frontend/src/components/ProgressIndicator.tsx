"use client";

import { Progress } from "@/components/ui/progress";
import type { ExtractionStatus } from "@/lib/types";

interface ProgressIndicatorProps {
  status: ExtractionStatus;
  progress: number;
  stepLabel: string;
  resultUrl: string | null;
  rowCount: number | null;
}

export function ProgressIndicator({
  status,
  progress,
  stepLabel,
  resultUrl,
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
        <div className="flex flex-col items-center gap-3 pt-2">
          <p className="text-sm text-gray-500">
            Encontrei <span className="font-medium text-gray-700">{rowCount}</span> registros!
          </p>
          <a
            href={resultUrl}
            download="timesheet.xlsx"
            className="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-blue-600 px-6 py-3 text-sm font-medium text-white transition-colors hover:bg-blue-700 sm:w-auto"
          >
            Baixar planilha Excel
          </a>
        </div>
      )}
    </div>
  );
}
