import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Extrator de Ponto",
  description:
    "Extraia registros de ponto de PDFs jurídicos trabalhistas e gere planilha Excel automaticamente.",
  icons: { icon: "/favicon.ico" },
  openGraph: {
    title: "Extrator de Ponto",
    description: "Extraia registros de ponto de PDFs jurídicos trabalhistas.",
    type: "website",
    locale: "pt_BR",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 5,
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="pt-BR">
      <body className={inter.className}>{children}</body>
    </html>
  );
}
