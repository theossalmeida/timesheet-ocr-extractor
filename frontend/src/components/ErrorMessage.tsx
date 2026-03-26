"use client";

interface ErrorMessageProps {
  message: string;
  onRetry: () => void;
}

export function ErrorMessage({ message, onRetry }: ErrorMessageProps) {
  return (
    <div className="rounded-xl border border-red-200 bg-red-50 p-4">
      <div className="flex items-start gap-3">
        <span className="text-xl">⚠️</span>
        <div className="flex-1">
          <p className="font-medium text-red-800">Erro ao processar PDF</p>
          <p className="mt-1 text-sm text-red-600">{message}</p>
        </div>
      </div>
      <button
        onClick={onRetry}
        className="mt-3 w-full rounded-lg border border-red-300 bg-white px-4 py-2 text-sm font-medium text-red-700 transition-colors hover:bg-red-50"
      >
        Tentar novamente
      </button>
    </div>
  );
}
