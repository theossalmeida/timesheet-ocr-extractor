"use client";

import { useCallback } from "react";
import { useDropzone } from "react-dropzone";
import { clsx } from "clsx";
import type { ExtractionStatus } from "@/lib/types";

interface UploadZoneProps {
  onFile: (file: File) => void;
  status: ExtractionStatus;
}

const MAX_SIZE = 50 * 1024 * 1024;

export function UploadZone({ onFile, status }: UploadZoneProps) {
  const disabled = status === "uploading" || status === "processing";

  const onDrop = useCallback(
    (accepted: File[]) => {
      if (accepted.length > 0) onFile(accepted[0]);
    },
    [onFile],
  );

  const { getRootProps, getInputProps, isDragActive, fileRejections } =
    useDropzone({
      onDrop,
      accept: { "application/pdf": [".pdf"] },
      maxSize: MAX_SIZE,
      maxFiles: 1,
      disabled,
    });

  const rejectionMessage = fileRejections[0]?.errors[0]?.message ?? null;

  if (status === "done") {
    return (
      <div className="flex flex-col items-center gap-2 rounded-xl border-2 border-green-400 bg-green-50 p-8 text-center">
        <div className="text-4xl">✓</div>
        <p className="font-medium text-green-700">Planilha gerada com sucesso!</p>
      </div>
    );
  }

  return (
    <div
      {...getRootProps()}
      className={clsx(
        "flex cursor-pointer flex-col items-center gap-3 rounded-xl border-2 border-dashed p-10 text-center transition-colors",
        isDragActive && "border-blue-500 bg-blue-50",
        !isDragActive && !disabled && "border-gray-300 hover:border-blue-400 hover:bg-gray-50",
        disabled && "cursor-not-allowed border-gray-200 bg-gray-50 opacity-60",
        (status === "error" || rejectionMessage) && "border-red-400 bg-red-50",
      )}
    >
      <input {...getInputProps()} />
      <div className="text-4xl text-gray-400">
        {isDragActive ? "📂" : "📄"}
      </div>
      {isDragActive ? (
        <p className="text-blue-600 font-medium">Solte o PDF aqui</p>
      ) : (
        <>
          <p className="font-medium text-gray-700">
            Arraste o PDF aqui ou{" "}
            <span className="text-blue-600 underline">clique para selecionar</span>
          </p>
          <p className="text-sm text-gray-400">Apenas PDF · máx. 50 MB</p>
        </>
      )}
      {rejectionMessage && (
        <p className="text-sm text-red-600">{rejectionMessage}</p>
      )}
    </div>
  );
}
