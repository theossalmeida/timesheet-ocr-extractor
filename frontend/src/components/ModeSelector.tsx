"use client";

import type { ExtractionMode } from "@/lib/types";

interface ModeSelectorProps {
  mode: ExtractionMode;
  onChange: (mode: ExtractionMode) => void;
  disabled?: boolean;
}

export function ModeSelector({ mode, onChange, disabled }: ModeSelectorProps) {
  return (
    <div className="flex rounded-lg border border-gray-200 p-1 gap-1">
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange("cartao")}
        className={`flex-1 rounded-md px-3 py-2 text-sm font-medium transition-colors ${
          mode === "cartao"
            ? "bg-blue-600 text-white shadow-sm"
            : "text-gray-600 hover:bg-gray-100"
        } disabled:opacity-50 disabled:cursor-not-allowed`}
      >
        Cartão de Ponto
      </button>
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange("guia")}
        className={`flex-1 rounded-md px-3 py-2 text-sm font-medium transition-colors ${
          mode === "guia"
            ? "bg-blue-600 text-white shadow-sm"
            : "text-gray-600 hover:bg-gray-100"
        } disabled:opacity-50 disabled:cursor-not-allowed`}
      >
        Guia Ministerial
      </button>
    </div>
  );
}
