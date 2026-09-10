import type { Metadata, Viewport } from "next";
import { Barlow, Barlow_Condensed } from "next/font/google";
import "./globals.css";

const body = Barlow({ subsets: ["latin"], weight: ["400", "500", "700"], variable: "--font-barlow" });
const heading = Barlow_Condensed({ subsets: ["latin"], weight: ["400", "600"], variable: "--font-barlow-condensed" });

export const metadata: Metadata = {
  title: "AUTUS",
  description:
    "Extraia registros de ponto de PDFs jurídicos trabalhistas e gere planilha Excel automaticamente.",
  icons: { icon: "/favicon.ico" },
  openGraph: {
    title: "AUTUS",
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
      <body className={`${body.className} ${body.variable} ${heading.variable}`}>{children}</body>
    </html>
  );
}
