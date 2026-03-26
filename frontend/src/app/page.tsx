"use client";

import { useExtraction } from "@/hooks/useExtraction";
import { UploadZone } from "@/components/UploadZone";
import { ProgressIndicator } from "@/components/ProgressIndicator";
import { ErrorMessage } from "@/components/ErrorMessage";

export default function Home() {
  const extraction = useExtraction();

  return (
    <main className="flex min-h-screen flex-col items-center justify-center bg-gray-50 p-4">
      <div className="w-full max-w-xl">
        <div className="mb-8 text-center">
          <h1 className="text-2xl font-bold text-gray-900">
            Extrator de Ponto
          </h1>
          <p className="mt-2 text-sm text-gray-500">
            Faça upload do PDF de registro de ponto e baixe a planilha Excel formatada.
          </p>
        </div>

        <div className="rounded-2xl bg-white p-6 shadow-sm ring-1 ring-gray-200 flex flex-col gap-4">
          {extraction.status !== "done" && (
            <UploadZone onFile={extraction.upload} status={extraction.status} />
          )}

          {extraction.status !== "idle" && extraction.status !== "error" && (
            <ProgressIndicator
              status={extraction.status}
              progress={extraction.progress}
              stepLabel={extraction.stepLabel}
              resultUrl={extraction.resultUrl}
              rowCount={extraction.rowCount}
              provider={extraction.provider}
            />
          )}

          {extraction.status === "error" && (
            <ErrorMessage
              message={extraction.error ?? "Erro desconhecido."}
              onRetry={extraction.reset}
            />
          )}

          {extraction.status === "done" && (
            <button
              onClick={extraction.reset}
              className="mt-2 w-full rounded-lg border border-gray-200 bg-gray-50 px-4 py-2 text-sm text-gray-600 transition-colors hover:bg-gray-100"
            >
              Processar outro PDF
            </button>
          )}
        </div>
      </div>
    </main>
  );
}
