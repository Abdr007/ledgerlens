"use client";

import { AnimatePresence, motion } from "framer-motion";
import { FileWarning, Loader2, UploadCloud } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface DropzoneProps {
  onFiles: (files: File[]) => void;
  disabled?: boolean;
  busy?: boolean;
  error?: string | null;
  className?: string;
}

/**
 * The drag-and-drop hero (spec §6).
 *
 * Drag state is tracked with a counter rather than a boolean: `dragenter` and
 * `dragleave` fire for every child element the pointer crosses, so a boolean
 * flickers the moment the cursor passes over the icon or the label.
 */
export function Dropzone({ onFiles, disabled, busy, error, className }: DropzoneProps) {
  const [dragDepth, setDragDepth] = React.useState(0);
  const inputRef = React.useRef<HTMLInputElement>(null);
  const isDragging = dragDepth > 0;

  const handleDrop = React.useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      setDragDepth(0);
      if (disabled) return;
      const files = Array.from(event.dataTransfer.files ?? []);
      if (files.length > 0) onFiles(files);
    },
    [disabled, onFiles],
  );

  const openPicker = React.useCallback(() => inputRef.current?.click(), []);

  return (
    <div className={className}>
      <div
        role="button"
        tabIndex={0}
        aria-label="Upload a document"
        aria-disabled={disabled}
        onDragEnter={(event) => {
          event.preventDefault();
          setDragDepth((depth) => depth + 1);
        }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={(event) => {
          event.preventDefault();
          setDragDepth((depth) => Math.max(0, depth - 1));
        }}
        onDrop={handleDrop}
        onClick={openPicker}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            openPicker();
          }
        }}
        className={cn(
          "group relative flex min-h-[210px] cursor-pointer flex-col items-center justify-center gap-4",
          "bevel border border-dashed px-6 py-10 text-center transition-all duration-300",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60",
          isDragging
            ? "border-accent bg-accent/[0.07] glow-accent scale-[1.005]"
            : "border-hairline-strong bg-white/[0.02] hover:border-accent/45 hover:bg-white/[0.035]",
          disabled && "pointer-events-none opacity-55",
        )}
      >
        <motion.div
          animate={isDragging ? { y: -6, scale: 1.08 } : { y: 0, scale: 1 }}
          transition={{ type: "spring", stiffness: 320, damping: 22 }}
          className={cn(
            "relative flex size-14 items-center justify-center transition-colors duration-300",
            isDragging ? "text-accent" : "text-ink-muted group-hover:text-accent",
          )}
        >
          <span
            className={cn(
              "absolute inset-[7px] rotate-45 border transition-colors duration-300",
              isDragging
                ? "border-accent/60 bg-accent/15"
                : "border-hairline bg-white/[0.04] group-hover:border-accent/45",
            )}
            aria-hidden
          />
          <span className="relative">
            {busy ? (
              <Loader2 className="size-5 animate-spin" />
            ) : (
              <UploadCloud className="size-5" strokeWidth={1.8} />
            )}
          </span>
        </motion.div>

        <div>
          <p className="text-[15px] font-semibold text-ink">
            {busy
              ? "Processing…"
              : isDragging
                ? "Release to ingest"
                : "Drop an invoice, receipt or contract"}
          </p>
          <p className="mt-1 text-xs text-ink-muted">
            PDF, PNG or JPEG · up to 10 MB · scans and phone photos welcome
          </p>
        </div>

        <Button
          variant="outline"
          size="sm"
          className="pointer-events-none"
          tabIndex={-1}
          type="button"
        >
          Browse files
        </Button>

        <input
          ref={inputRef}
          type="file"
          accept="application/pdf,image/png,image/jpeg,.pdf,.png,.jpg,.jpeg"
          multiple
          className="hidden"
          onChange={(event) => {
            const files = Array.from(event.target.files ?? []);
            if (files.length > 0) onFiles(files);
            // Reset so re-selecting the same file fires `change` again.
            event.target.value = "";
          }}
        />
      </div>

      <AnimatePresence>
        {error ? (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            <div className="mt-3 flex items-start gap-2 border-l-2 border-flag bg-flag/[0.07] px-3 py-2.5">
              <FileWarning className="mt-0.5 size-4 shrink-0 text-flag" />
              <p className="text-[13px] leading-relaxed text-flag/90">{error}</p>
            </div>
          </motion.div>
        ) : null}
      </AnimatePresence>
    </div>
  );
}
