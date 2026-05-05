"use client";

import type { ExtractionMode } from "@/lib/types";

interface ModeSelectorProps {
  mode: ExtractionMode;
  onChange: (mode: ExtractionMode) => void;
  disabled?: boolean;
}

const TABS: Array<{ key: ExtractionMode; label: string }> = [
  { key: "cartao", label: "Cartao de Ponto" },
  { key: "guia", label: "Guia Ministerial" },
  { key: "contracheque", label: "Contracheque" },
  { key: "horas_extras", label: "Horas Extras" },
];

export function ModeSelector({ mode, onChange, disabled }: ModeSelectorProps) {
  return (
    <div className="grid grid-cols-2 gap-1 rounded-lg border border-gray-200 p-1 sm:flex">
      {TABS.map(({ key, label }) => (
        <button
          key={key}
          type="button"
          disabled={disabled}
          onClick={() => onChange(key)}
          className={`rounded-md px-2 py-2 text-xs font-medium transition-colors sm:flex-1 sm:px-3 sm:text-sm ${
            mode === key
              ? "bg-blue-600 text-white shadow-sm"
              : "text-gray-600 hover:bg-gray-100"
          } disabled:cursor-not-allowed disabled:opacity-50`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
