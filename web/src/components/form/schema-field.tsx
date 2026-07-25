"use client";

import { TriangleAlert } from "lucide-react";

import {
  FieldError,
  FieldHint,
  Input,
  Label,
  Segmented,
  Select,
  Textarea,
} from "@/components/ui/field";
import type { FormField } from "@/lib/schema-form";

/**
 * One control, derived from one JSON Schema property.
 *
 * The interesting cases:
 *
 * **A required boolean gets a segmented control with nothing selected**, not a checkbox. A
 * checkbox answers its own question the moment it renders, so "required" would mean
 * nothing -- the form would submit `false` for somebody who never read the field. The Redis
 * path marks `eviction` required specifically so a person has to decide whether the thing
 * they are asking for is a cache or a store, and a pre-answered control throws that away.
 *
 * **An optional value left alone is omitted from the request**, not sent empty. The broker
 * fills in the schema's default and records what it filled in, so omitting a field records
 * "they did not choose" while sending "" would record a choice nobody made.
 *
 * **A property this generator cannot render becomes a JSON textarea with a visible note.**
 * Ugly on purpose. Silently dropping a property the broker will validate is the drift this
 * whole approach exists to prevent, so when it happens it should be impossible to miss.
 */
export function SchemaField({
  field,
  value,
  error,
  onChange,
}: {
  field: FormField;
  value: string | null;
  error?: string;
  onChange: (value: string | null) => void;
}) {
  const id = `field-${field.name}`;
  const describedBy = `${id}-hint`;

  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-3">
        <Label htmlFor={id} required={field.required}>
          <span className="font-mono text-[13px]">{field.name}</span>
        </Label>
        {!field.required && field.defaultValue !== null ? (
          <span className="text-2xs text-muted-foreground">
            defaults to <span className="font-mono">{field.defaultValue}</span>
          </span>
        ) : null}
      </div>

      {field.description ? (
        <FieldHint id={describedBy}>{field.description}</FieldHint>
      ) : null}

      <Control field={field} id={id} value={value} error={error} onChange={onChange} />

      {field.constraints.length > 0 && field.kind !== "enum" ? (
        <FieldHint className="font-mono text-2xs">{field.constraints.join(" · ")}</FieldHint>
      ) : null}

      <FieldError>{error}</FieldError>
    </div>
  );
}

function Control({
  field,
  id,
  value,
  error,
  onChange,
}: {
  field: FormField;
  id: string;
  value: string | null;
  error?: string;
  onChange: (value: string | null) => void;
}) {
  const invalid = Boolean(error);

  switch (field.kind) {
    case "enum":
      return (
        <Select
          id={id}
          aria-invalid={invalid}
          value={value ?? ""}
          onChange={(event) => onChange(event.target.value || null)}
        >
          <option value="">{field.required ? "Choose one…" : "Leave unset"}</option>
          {field.options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </Select>
      );

    case "boolean":
      return (
        <div className="pt-0.5">
          <Segmented
            name={field.name}
            invalid={invalid}
            value={value}
            onChange={onChange}
            options={[
              { value: "true", label: "Yes" },
              { value: "false", label: "No" },
            ]}
          />
        </div>
      );

    case "number":
      return (
        <Input
          id={id}
          type="number"
          inputMode="decimal"
          aria-invalid={invalid}
          value={value ?? ""}
          min={typeof field.schema.minimum === "number" ? field.schema.minimum : undefined}
          max={typeof field.schema.maximum === "number" ? field.schema.maximum : undefined}
          onChange={(event) => onChange(event.target.value || null)}
        />
      );

    case "list":
      return (
        <Textarea
          id={id}
          rows={2}
          aria-invalid={invalid}
          value={value ?? ""}
          placeholder="One per line, or comma separated"
          onChange={(event) => onChange(event.target.value || null)}
        />
      );

    case "json":
      return (
        <div className="space-y-1.5">
          <div className="flex items-start gap-2 rounded-md border border-warn/30 bg-warn-soft px-2.5 py-2 text-xs leading-relaxed text-warn">
            <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            <span>
              This dashboard could not derive a control for this property: {field.fallbackReason}
              . Enter its value as JSON. The broker validates it either way.
            </span>
          </div>
          <Textarea
            id={id}
            rows={4}
            spellCheck={false}
            aria-invalid={invalid}
            className="font-mono text-[12px]"
            value={value ?? ""}
            onChange={(event) => onChange(event.target.value || null)}
          />
        </div>
      );

    case "text":
      return (
        <Input
          id={id}
          aria-invalid={invalid}
          value={value ?? ""}
          spellCheck={false}
          maxLength={
            typeof field.schema.maxLength === "number" ? field.schema.maxLength : undefined
          }
          onChange={(event) => onChange(event.target.value || null)}
        />
      );
  }
}
