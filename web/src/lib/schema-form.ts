import type { JsonSchema } from "@/lib/types";

/**
 * Turning a golden path's JSON Schema into a form, at runtime, every time.
 *
 * **Why this file exists at all.** The whole premise of bailment is that one YAML file
 * produces the MCP tool, the OSB catalog entry, the policy context and the dashboard form.
 * A hand-written React form per golden path would put a second definition of "what
 * arguments does this take" in a repository whose entire thesis is that a second
 * definition drifts. It would drift here first and worst: somebody adds a required input
 * to a path, the agent's tool picks it up automatically because it is generated, and the
 * human form silently keeps submitting requests that the broker now rejects.
 *
 * So the form is derived, always, from `CatalogEntry.input_schema` -- which is
 * `GoldenPath.input_schema_with_lease()`, the same object the agent's tool is built from.
 *
 * **Nothing is ever silently dropped.** A keyword this generator does not recognise
 * produces a `json` field: a textarea with a visible note saying the dashboard could not
 * derive a control for it. That is deliberately a little ugly. A form that quietly omits a
 * property the broker will validate is the exact failure this file is here to prevent, and
 * it should be obvious when it happens rather than discovered from a 400.
 *
 * **Client-side validation is a courtesy, not the enforcement.** It mirrors
 * `bailment.engine.validation` closely enough to catch a typo before a round trip, in the
 * same order (type, then enum, then length, then pattern) so that the message a user sees
 * first is the message the broker would have sent. The broker remains the authority and
 * its `problems` list is mapped back onto the fields in the request dialog.
 */

export type FieldKind = "text" | "enum" | "number" | "boolean" | "list" | "json";

export interface EnumOption {
  /** The form's internal value. Always a string, because DOM controls only hold strings. */
  value: string;
  /** The value that goes on the wire, in whatever type the schema declared. */
  raw: unknown;
  label: string;
}

export interface FormField {
  name: string;
  kind: FieldKind;
  label: string;
  description: string;
  required: boolean;
  schema: JsonSchema;
  /** Present for `enum`. */
  options: EnumOption[];
  /** The schema default, rendered as a hint and pre-filled when there is one. */
  defaultValue: string | null;
  /** Short human constraints, shown under the control. "3–40 characters", "lowercase". */
  constraints: string[];
  /** Why this field fell back to raw JSON. Empty unless `kind` is `json`. */
  fallbackReason: string;
}

export interface GeneratedForm {
  fields: FormField[];
  /**
   * The universal `ttl` argument, peeled off. It is advertised inside the input schema
   * because an MCP tool has exactly one argument object, but it is not one of the path's
   * inputs -- the broker's own `_split_lease_arguments` removes it before the closed
   * schema ever sees it. The dashboard sends it as the request body's `ttl` field and
   * gives it a purpose-built control, because "how long do I get this for" deserves more
   * than a text box with a regex.
   */
  ttl: JsonSchema | null;
}

/** Form state. Every value is a string or null; null means "the user has not set this". */
export type FormValues = Record<string, string | null>;

function typeNames(schema: JsonSchema): string[] {
  const declared = schema.type;
  if (typeof declared === "string") return [declared];
  if (Array.isArray(declared)) return declared.filter((t): t is string => typeof t === "string");
  return [];
}

function describeConstraints(schema: JsonSchema): string[] {
  const out: string[] = [];
  const { minLength, maxLength, pattern, minimum, maximum, minItems, maxItems } = schema;

  if (typeof minLength === "number" && typeof maxLength === "number") {
    out.push(`${minLength}–${maxLength} characters`);
  } else if (typeof maxLength === "number") {
    out.push(`up to ${maxLength} characters`);
  } else if (typeof minLength === "number") {
    out.push(`at least ${minLength} characters`);
  }

  if (typeof minimum === "number" && typeof maximum === "number") {
    out.push(`between ${minimum} and ${maximum}`);
  } else if (typeof minimum === "number") {
    out.push(`at least ${minimum}`);
  } else if (typeof maximum === "number") {
    out.push(`at most ${maximum}`);
  }

  if (typeof minItems === "number") out.push(`at least ${minItems} entries`);
  if (typeof maxItems === "number") out.push(`at most ${maxItems} entries`);
  if (typeof pattern === "string") out.push(`must match ${pattern}`);

  return out;
}

function toOption(raw: unknown): EnumOption {
  const label = typeof raw === "string" ? raw : JSON.stringify(raw);
  return { value: String(label), raw, label: String(label) };
}

function classify(name: string, schema: JsonSchema, required: boolean): FormField {
  const types = typeNames(schema);
  const base = {
    name,
    label: typeof schema.title === "string" ? schema.title : name,
    description: typeof schema.description === "string" ? schema.description : "",
    required,
    schema,
    options: [] as EnumOption[],
    defaultValue: schema.default === undefined ? null : String(schema.default),
    constraints: describeConstraints(schema),
    fallbackReason: "",
  };

  // A composed schema is a decision this generator is not qualified to make on somebody's
  // behalf. Falling back is honest; guessing at the first branch is not.
  if (schema.oneOf || schema.anyOf || schema.allOf || schema.$ref) {
    return {
      ...base,
      kind: "json",
      fallbackReason:
        "this property uses a composed schema (oneOf / anyOf / allOf / $ref), which this " +
        "form cannot render as a single control",
    };
  }

  if (Array.isArray(schema.enum) && schema.enum.length > 0) {
    return { ...base, kind: "enum", options: schema.enum.map(toOption) };
  }

  if (types.includes("boolean")) return { ...base, kind: "boolean" };
  if (types.includes("number") || types.includes("integer")) return { ...base, kind: "number" };

  if (types.includes("array")) {
    const itemTypes = schema.items ? typeNames(schema.items) : [];
    const scalar = itemTypes.some((t) => ["string", "number", "integer"].includes(t));
    if (scalar || itemTypes.length === 0) return { ...base, kind: "list" };
    return {
      ...base,
      kind: "json",
      fallbackReason: "this property is a list of objects, which needs a raw JSON value",
    };
  }

  if (types.includes("object")) {
    return {
      ...base,
      kind: "json",
      fallbackReason: "this property is a nested object, which needs a raw JSON value",
    };
  }

  if (types.length === 0) {
    return {
      ...base,
      kind: "json",
      fallbackReason: "this property declares no type, so no control can be derived for it",
    };
  }

  return { ...base, kind: "text" };
}

/** Derive the whole form from a golden path's advertised input schema. */
export function buildForm(schema: JsonSchema): GeneratedForm {
  const properties = schema.properties ?? {};
  const required = new Set(schema.required ?? []);
  const fields: FormField[] = [];
  let ttl: JsonSchema | null = null;

  for (const [name, property] of Object.entries(properties)) {
    if (name === "ttl") {
      ttl = property;
      continue;
    }
    fields.push(classify(name, property, required.has(name)));
  }

  // Required fields first, then the order the schema declared. An operator filling this in
  // under time pressure should meet the mandatory questions before the optional ones.
  fields.sort((a, b) => Number(b.required) - Number(a.required));
  return { fields, ttl };
}

/** Initial state: schema defaults where they exist, unset everywhere else. */
export function initialValues(form: GeneratedForm): FormValues {
  const values: FormValues = {};
  for (const field of form.fields) values[field.name] = field.defaultValue;
  return values;
}

// --------------------------------------------------------------------------------------
// Validation -- same order as bailment.engine.validation, so the same message comes first
// --------------------------------------------------------------------------------------

function validateString(schema: JsonSchema, value: string): string | null {
  const { maxLength, minLength, pattern } = schema;
  if (typeof maxLength === "number" && value.length > maxLength) {
    return `is ${value.length} characters, maximum is ${maxLength}`;
  }
  if (typeof minLength === "number" && value.length < minLength) {
    return `is ${value.length} characters, minimum is ${minLength}`;
  }
  if (typeof pattern === "string") {
    try {
      // Unanchored, matching the broker's use of `re.search`. A schema that writes ^...$
      // -- as every shipped path does -- is anchored either way.
      if (!new RegExp(pattern).test(value)) return `does not match the required pattern ${pattern}`;
    } catch {
      // A Python regex JavaScript cannot compile. The broker still validates it; refusing
      // to submit because this runtime lacks a syntax would be the wrong failure.
      return null;
    }
  }
  return null;
}

function validateNumber(schema: JsonSchema, value: number): string | null {
  const { minimum, maximum, exclusiveMinimum, exclusiveMaximum, multipleOf } = schema;
  if (typeof minimum === "number" && value < minimum) return `must be at least ${minimum}`;
  if (typeof maximum === "number" && value > maximum) return `must be at most ${maximum}`;
  if (typeof exclusiveMinimum === "number" && value <= exclusiveMinimum) {
    return `must be greater than ${exclusiveMinimum}`;
  }
  if (typeof exclusiveMaximum === "number" && value >= exclusiveMaximum) {
    return `must be less than ${exclusiveMaximum}`;
  }
  if (typeof multipleOf === "number" && multipleOf > 0) {
    const ratio = value / multipleOf;
    if (Math.abs(ratio - Math.round(ratio)) > 1e-9) return `must be a multiple of ${multipleOf}`;
  }
  return null;
}

/** One field's problem, phrased the way the broker phrases it, or null if it is fine. */
export function validateField(field: FormField, raw: string | null): string | null {
  const empty = raw === null || raw.trim() === "";

  if (empty) return field.required ? "is required" : null;
  const value = raw as string;

  switch (field.kind) {
    case "enum": {
      const known = field.options.some((option) => option.value === value);
      return known
        ? null
        : `${value} is not one of ${field.options.map((o) => o.label).join(", ")}`;
    }
    case "boolean":
      return value === "true" || value === "false" ? null : "must be true or false";
    case "number": {
      const parsed = Number(value);
      if (!Number.isFinite(parsed)) return "must be a number";
      if (typeNames(field.schema).includes("integer") && !Number.isInteger(parsed)) {
        return "must be a whole number";
      }
      return validateNumber(field.schema, parsed);
    }
    case "list": {
      const items = splitList(value);
      const { minItems, maxItems } = field.schema;
      if (typeof minItems === "number" && items.length < minItems) {
        return `needs at least ${minItems} entries`;
      }
      if (typeof maxItems === "number" && items.length > maxItems) {
        return `takes at most ${maxItems} entries`;
      }
      const itemSchema = field.schema.items;
      if (itemSchema && typeNames(itemSchema).includes("string")) {
        for (const item of items) {
          const problem = validateString(itemSchema, item);
          if (problem) return `${item}: ${problem}`;
        }
      }
      return null;
    }
    case "json":
      try {
        JSON.parse(value);
        return null;
      } catch (error) {
        return `is not valid JSON (${error instanceof Error ? error.message : "parse failed"})`;
      }
    case "text":
      return validateString(field.schema, value);
  }
}

export function validateAll(fields: FormField[], values: FormValues): Record<string, string> {
  const problems: Record<string, string> = {};
  for (const field of fields) {
    const problem = validateField(field, values[field.name] ?? null);
    if (problem) problems[field.name] = problem;
  }
  return problems;
}

function splitList(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

/**
 * Form state to request body.
 *
 * Unset optional values are omitted rather than sent as null or "". The broker applies the
 * schema's own defaults in `validate_inputs`, and the values it fills in are the ones the
 * audit trail will show -- so omitting a field the user did not touch records what
 * actually happened, while sending an empty string records a choice nobody made.
 */
export function toInputs(fields: FormField[], values: FormValues): Record<string, unknown> {
  const inputs: Record<string, unknown> = {};

  for (const field of fields) {
    const raw = values[field.name];
    if (raw === null || raw === undefined || raw.trim() === "") continue;

    switch (field.kind) {
      case "enum": {
        const option = field.options.find((candidate) => candidate.value === raw);
        inputs[field.name] = option ? option.raw : raw;
        break;
      }
      case "boolean":
        inputs[field.name] = raw === "true";
        break;
      case "number":
        inputs[field.name] = Number(raw);
        break;
      case "list": {
        const itemSchema = field.schema.items;
        const numeric = itemSchema
          ? typeNames(itemSchema).some((t) => t === "number" || t === "integer")
          : false;
        inputs[field.name] = numeric ? splitList(raw).map(Number) : splitList(raw);
        break;
      }
      case "json":
        try {
          inputs[field.name] = JSON.parse(raw);
        } catch {
          // Unreachable through the dialog, which validates before it submits. If it does
          // happen, sending the string means the broker's schema check reports it rather
          // than the field vanishing from the request.
          inputs[field.name] = raw;
        }
        break;
      case "text":
        inputs[field.name] = raw;
        break;
    }
  }

  return inputs;
}

/**
 * Map the broker's `problems` list back onto fields.
 *
 * `validate_inputs` prefixes every problem with its location, rooted at `input` --
 * `input.subdomain: 'Admin' does not match ...`. Anything that does not name a field this
 * form knows about is returned in `general`, because a validation message nobody sees is
 * worse than one in the wrong place.
 */
export function assignProblems(
  fields: FormField[],
  problems: string[],
): { byField: Record<string, string>; general: string[] } {
  const byField: Record<string, string> = {};
  const general: string[] = [];

  for (const problem of problems) {
    const match = /^input\.([A-Za-z0-9_]+)\b\s*:?\s*(.*)$/.exec(problem);
    const name = match?.[1];
    if (name && fields.some((field) => field.name === name)) {
      byField[name] = match?.[2]?.trim() || problem;
    } else {
      general.push(problem);
    }
  }

  return { byField, general };
}

// --------------------------------------------------------------------------------------
// TTL
// --------------------------------------------------------------------------------------

const DURATION_RE = /^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$/;

/** Parse the broker's compact duration form. Returns null for anything it would reject. */
export function parseDuration(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  const match = DURATION_RE.exec(trimmed);
  if (!match) return null;
  const seconds =
    Number(match[1] ?? 0) * 3600 + Number(match[2] ?? 0) * 60 + Number(match[3] ?? 0);
  return seconds > 0 ? seconds : null;
}

/** Render seconds back into the compact form, round-tripping `parseDuration`. */
export function toDurationString(seconds: number): string {
  const total = Math.max(0, Math.trunc(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const parts = [hours ? `${hours}h` : "", minutes ? `${minutes}m` : "", secs ? `${secs}s` : ""];
  return parts.join("") || "0s";
}
