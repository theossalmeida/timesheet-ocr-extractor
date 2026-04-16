"use client";

import type { ExtractionMode } from "@/lib/types";

interface ModeSelectorProps {
  mode: ExtractionMode;
  onChange: (mode: ExtractionMode) => void;
  disabled?: boolean;
}

const TABS: Array<{ key: ExtractionMode; label: string }> = [
  { key: "cartao", label: "Cartão de Ponto" },
  { key: "guia", label: "Guia Ministerial" },
  { key: "contracheque", label: "Contracheque" },
];

export function ModeSelector({ mode, onChange, disabled }: ModeSelectorProps) {
  return (
    <div className="flex rounded-lg border border-gray-200 p-1 gap-1">
      {TABS.map(({ key, label }) => (
        <button
          key={key}
          type="button"
          disabled={disabled}
          onClick={() => onChange(key)}
          className={`flex-1 rounded-md px-3 py-2 text-sm font-medium transition-colors ${
            mode === key
              ? "bg-blue-600 text-white shadow-sm"
              : "text-gray-600 hover:bg-gray-100"
          } disabled:opacity-50 disabled:cursor-not-allowed`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
