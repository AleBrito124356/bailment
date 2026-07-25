"use client";

import * as React from "react";
import { ChevronDown } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Form controls.
 *
 * The select is a native `<select>`, not a Radix listbox. That is a considered choice: a
 * native control is keyboard-perfect on every platform without any code, it works with
 * whatever assistive technology the operator already has configured, and the enum lists in
 * this product are three items long. A custom listbox would be four hundred bytes of
 * JavaScript to make `dev / staging / prod` marginally prettier.
 */

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...props }, ref) {
    return (
      <input
        ref={ref}
        className={cn(
          "h-9 w-full rounded-md border border-input bg-background px-3 text-sm " +
            "placeholder:text-muted-foreground/70 transition-colors " +
            "focus-visible:border-accent focus-visible:outline-none focus-visible:ring-2 " +
            "focus-visible:ring-accent/20 focus-visible:ring-offset-0 " +
            "disabled:cursor-not-allowed disabled:bg-muted disabled:text-muted-foreground " +
            "aria-[invalid=true]:border-danger aria-[invalid=true]:ring-danger/15",
          className,
        )}
        {...props}
      />
    );
  },
);

export const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(function Textarea({ className, ...props }, ref) {
  return (
    <textarea
      ref={ref}
      className={cn(
        "w-full rounded-md border border-input bg-background px-3 py-2 text-sm leading-relaxed " +
          "placeholder:text-muted-foreground/70 transition-colors " +
          "focus-visible:border-accent focus-visible:outline-none focus-visible:ring-2 " +
          "focus-visible:ring-accent/20 focus-visible:ring-offset-0 " +
          "aria-[invalid=true]:border-danger aria-[invalid=true]:ring-danger/15",
        className,
      )}
      {...props}
    />
  );
});

export const Select = React.forwardRef<
  HTMLSelectElement,
  React.SelectHTMLAttributes<HTMLSelectElement>
>(function Select({ className, children, ...props }, ref) {
  return (
    <div className="relative">
      <select
        ref={ref}
        className={cn(
          "h-9 w-full appearance-none rounded-md border border-input bg-background pl-3 pr-8 " +
            "text-sm transition-colors focus-visible:border-accent focus-visible:outline-none " +
            "focus-visible:ring-2 focus-visible:ring-accent/20 focus-visible:ring-offset-0 " +
            "disabled:cursor-not-allowed disabled:bg-muted disabled:text-muted-foreground " +
            "aria-[invalid=true]:border-danger aria-[invalid=true]:ring-danger/15",
          className,
        )}
        {...props}
      >
        {children}
      </select>
      <ChevronDown
        className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
        aria-hidden
      />
    </div>
  );
});

export function Label({
  className,
  required,
  children,
  ...props
}: React.LabelHTMLAttributes<HTMLLabelElement> & { required?: boolean }) {
  return (
    <label className={cn("text-sm font-medium text-foreground", className)} {...props}>
      {children}
      {required ? (
        <span className="ml-1 text-accent" title="Required">
          *
        </span>
      ) : null}
    </label>
  );
}

export function FieldHint({ className, ...props }: React.HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn("text-xs leading-relaxed text-muted-foreground", className)} {...props} />;
}

export function FieldError({ children }: { children: React.ReactNode }) {
  if (!children) return null;
  return (
    <p className="text-xs font-medium text-danger" role="alert">
      {children}
    </p>
  );
}

export interface SegmentedOption {
  value: string;
  label: string;
}

/**
 * A two- or three-way choice with no default selection.
 *
 * This is what renders a required boolean, and the absence of a pre-selected option is the
 * point. A checkbox is pre-answered "no" the moment it appears, so a required boolean
 * rendered as a checkbox is a question the form answers on the user's behalf. The Redis
 * golden path makes `eviction` required precisely so that somebody has to decide whether
 * this is a cache or a store; a control that quietly submits `false` defeats that.
 */
export function Segmented({
  options,
  value,
  onChange,
  name,
  invalid = false,
}: {
  options: SegmentedOption[];
  value: string | null;
  onChange: (value: string | null) => void;
  name: string;
  invalid?: boolean;
}) {
  return (
    <div
      role="radiogroup"
      aria-label={name}
      aria-invalid={invalid}
      className={cn(
        "inline-flex rounded-md border border-input bg-background p-0.5",
        invalid && "border-danger",
      )}
    >
      {options.map((option) => {
        const selected = value === option.value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={() => onChange(selected ? null : option.value)}
            className={cn(
              "rounded-[5px] px-3 py-1 text-[13px] font-medium transition-colors",
              selected
                ? "bg-accent text-accent-foreground"
                : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
