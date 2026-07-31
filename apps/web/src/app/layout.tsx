import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono, Instrument_Serif } from "next/font/google";

import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });
// Used sparingly — headline and panel titles only. The page is mono-dominant:
// everything measured stays in Geist Mono, and the serif is the human voice.
const instrumentSerif = Instrument_Serif({
  variable: "--font-instrument-serif",
  subsets: ["latin"],
  weight: "400",
});

export const metadata: Metadata = {
  title: "LedgerLens — Intelligent Document Processing",
  description:
    "Vision-LLM extraction with deterministic validation, explainable anomaly detection " +
    "and a full audit trail. LLMs extract, code verifies.",
  applicationName: "LedgerLens",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: "#050b0a",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={`${geistSans.variable} ${instrumentSerif.variable} ${geistMono.variable} antialiased`}>
        <div className="backdrop-grid" aria-hidden />
        <div className="scanlines" aria-hidden />
        {children}
      </body>
    </html>
  );
}
