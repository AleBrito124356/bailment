"use client";

import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * There is one primary button style and it is the accent blue. Everything else is a
 * bordered white surface or plain text.
 *
 * `danger` is an outline, not a filled red block. The destructive actions in this product
 * -- revoke a lease, reject a request -- are ordinary parts of running it, and a screen
 * that renders them as emergencies makes an operator hesitate over the routine ones.
 */
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-md font-medium " +
    "transition-colors duration-100 disabled:pointer-events-none disabled:opacity-50 " +
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring " +
    "focus-visible:ring-offset-2 focus-visible:ring-offset-background",
  {
    variants: {
      variant: {
        primary: "bg-accent text-accent-foreground hover:bg-accent/90 active:bg-accent/95",
        secondary:
          "border border-border bg-card text-foreground hover:bg-muted/60 active:bg-muted",
        ghost: "text-muted-foreground hover:bg-muted hover:text-foreground",
        danger:
          "border border-danger/30 bg-card text-danger hover:bg-danger-soft " +
          "hover:border-danger/50",
        link: "text-accent underline-offset-4 hover:underline",
      },
      size: {
        sm: "h-8 px-2.5 text-[13px]",
        md: "h-9 px-3.5 text-sm",
        lg: "h-10 px-4 text-sm",
        icon: "h-8 w-8",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
  /** Shows a spinner and disables the button. Never changes its width. */
  loading?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant, size, asChild = false, loading = false, children, disabled, ...props },
  ref,
) {
  if (asChild) {
    // Slot requires exactly one element child, and it counts a null as a child. Rendering
    // the spinner slot here -- even as `null` -- makes every `<Button asChild>` in the
    // dashboard throw "Slot failed to slot onto its children" at render time. There is
    // nowhere to put a spinner on a borrowed element anyway, so `loading` on an asChild
    // button only disables it.
    return (
      <Slot
        ref={ref}
        className={cn(buttonVariants({ variant, size }), className)}
        {...props}
      >
        {children}
      </Slot>
    );
  }
  return (
    <button
      ref={ref}
      className={cn(buttonVariants({ variant, size }), className)}
      disabled={disabled || loading}
      {...props}
    >
      {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
      {children}
    </button>
  );
});

export { buttonVariants };
