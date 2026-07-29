import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "LedgerLens — Intelligent Document Processing",
  description:
    "Vision-LLM extraction with deterministic validation, explainable anomaly detection " +
    "and a full audit trail. LLMs extract, code verifies.",
  applicationName: "LedgerLens",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: "#07060d",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={`${geistSans.variable} ${geistMono.variable} antialiased`}>
        <div className="backdrop-grid" aria-hidden />
        <div className="scanlines" aria-hidden />
        {children}
      </body>
    </html>
  );
}
