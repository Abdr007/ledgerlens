"use client";

import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "bevel-sm inline-flex items-center justify-center gap-2 whitespace-nowrap font-mono text-xs " +
    "font-medium uppercase tracking-[0.1em] transition-all duration-150 outline-none " +
    "focus-visible:ring-2 focus-visible:ring-accent/70 focus-visible:ring-offset-2 " +
    "focus-visible:ring-offset-base disabled:pointer-events-none disabled:opacity-45 " +
    "[&_svg]:size-3.5 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        primary:
          "bg-accent text-base font-bold hover:brightness-110 active:translate-y-px " +
          "shadow-[0_0_20px_-6px_rgb(79_209_165/0.8)]",
        outline:
          "border border-hairline-strong bg-white/[0.025] text-ink hover:border-accent/60 " +
          "hover:bg-accent/[0.07] hover:text-accent",
        ghost: "text-ink-muted hover:bg-white/[0.05] hover:text-accent",
        approve:
          "border border-pass/40 bg-pass/[0.08] text-pass hover:bg-pass/20 " +
          "hover:border-pass/70 active:translate-y-px",
        reject:
          "border border-flag/40 bg-flag/[0.08] text-flag hover:bg-flag/20 " +
          "hover:border-flag/70 active:translate-y-px",
      },
      size: {
        sm: "h-8 px-3 text-xs",
        md: "h-10 px-4",
        lg: "h-12 px-6 text-base",
        icon: "size-9 [&_svg]:size-4",
      },
    },
    defaultVariants: { variant: "outline", size: "md" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export function Button({
  className,
  variant,
  size,
  asChild = false,
  ...props
}: ButtonProps) {
  const Comp = asChild ? Slot : "button";
  return <Comp className={cn(buttonVariants({ variant, size }), className)} {...props} />;
}

export { buttonVariants };
