"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * A rosette drawn from a document's SHA-256.
 *
 * Guilloché — the interference pattern engraved on banknotes and share
 * certificates — exists because it is hard to reproduce: the pattern proves the
 * document in your hand is the original. That is the same claim this pipeline
 * makes with a content hash, so the seal is drawn *from* the hash rather than
 * decorated beside it. Every parameter below is read out of the digest.
 *
 * Which makes it true rather than ornamental, and worth watching:
 *
 * - the same bytes always produce the same seal, which is what idempotent
 *   ingestion means — upload a file twice and the second seal is indistinguishable
 * - one different byte produces a completely different figure
 * - a seal cannot be drawn for a document that has not been hashed
 *
 * Pure SVG, no dependency, no animation loop. `prefers-reduced-motion` is
 * respected by having nothing to reduce.
 */

/** Read `length` hex characters at `offset`, wrapping, as an integer. */
function read(hash: string, offset: number, length: number): number {
  const clean = hash.replace(/[^0-9a-f]/gi, "").toLowerCase();
  if (clean.length === 0) return 0;
  let out = "";
  for (let i = 0; i < length; i += 1) out += clean[(offset + i) % clean.length];
  return Number.parseInt(out, 16);
}

function scale(value: number, max: number, low: number, high: number): number {
  return low + ((value % max) / max) * (high - low);
}

export interface HashSealProps {
  /** The document's SHA-256. A short prefix works; more digest, more variation. */
  hash: string | null | undefined;
  size?: number;
  className?: string;
  /** Verified documents seal in verdigris; anything held for a human seals amber. */
  tone?: "accent" | "warn" | "flag";
}

export function HashSeal({ hash, size = 76, className, tone = "accent" }: HashSealProps) {
  const figure = React.useMemo(() => {
    if (!hash) return null;

    // Each parameter comes from its own slice of the digest, so two documents
    // sharing a prefix still diverge in the outer rings.
    const petals = 7 + (read(hash, 0, 2) % 12);
    const inner = scale(read(hash, 2, 3), 4096, 0.3, 0.62);
    const twist = scale(read(hash, 5, 3), 4096, 0, Math.PI * 2);
    const rings = 2 + (read(hash, 8, 1) % 3);
    const skew = scale(read(hash, 9, 2), 256, 0.82, 1.18);

    const paths: string[] = [];
    for (let ring = 0; ring < rings; ring += 1) {
      const radius = 46 - ring * scale(read(hash, 11 + ring, 2), 256, 7, 12);
      const lobes = petals + ring * (1 + (read(hash, 14 + ring, 1) % 3));
      const phase = twist + ring * 0.7;
      const steps = 360;
      let d = "";
      for (let step = 0; step <= steps; step += 1) {
        const t = (step / steps) * Math.PI * 2;
        // A hypotrochoid: the curve an engine-turning lathe actually traces.
        const r = radius * (inner + (1 - inner) * Math.abs(Math.cos((lobes * t) / 2)));
        const x = 50 + r * Math.cos(t + phase) * skew;
        const y = 50 + r * Math.sin(t + phase);
        d += `${step === 0 ? "M" : "L"}${x.toFixed(2)} ${y.toFixed(2)}`;
      }
      paths.push(`${d}Z`);
    }
    return { paths, petals };
  }, [hash]);

  const stroke =
    tone === "warn"
      ? "var(--color-warn)"
      : tone === "flag"
        ? "var(--color-flag)"
        : "var(--color-accent)";

  if (!figure) {
    // No hash, no seal. A placeholder ring would imply a document has been
    // fingerprinted when it has not.
    return (
      <div
        aria-hidden
        className={cn("shrink-0 rounded-full border border-dashed border-hairline", className)}
        style={{ width: size, height: size }}
      />
    );
  }

  return (
    <svg
      viewBox="0 0 100 100"
      width={size}
      height={size}
      className={cn("shrink-0 overflow-visible", className)}
      role="img"
      aria-label="Content seal, drawn from this document's SHA-256"
    >
      <circle cx="50" cy="50" r="47.5" fill="none" stroke="var(--color-copper)" strokeOpacity="0.5" strokeWidth="0.6" />
      <circle cx="50" cy="50" r="44" fill="none" stroke="var(--color-copper)" strokeOpacity="0.22" strokeWidth="0.4" />
      {figure.paths.map((d, index) => (
        <path
          key={d.slice(0, 24) + index}
          d={d}
          fill="none"
          stroke={stroke}
          strokeOpacity={0.9 - index * 0.24}
          strokeWidth={0.55 - index * 0.1}
          strokeLinejoin="round"
        />
      ))}
    </svg>
  );
}
