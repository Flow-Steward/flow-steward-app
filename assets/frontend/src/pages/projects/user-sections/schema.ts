import type { JsonSchema, ProjectUserSection, SectionFieldConfig } from "./types";

/** Form kinds a schema node supports; the server applies the same rules (ADR 0075). */
export type FieldKind =
  | "text"
  | "text_long"
  | "enum"
  | "number"
  | "boolean"
  | "file"
  | "string_list"
  | "enum_list"
  | "number_list"
  | "object"
  | "object_list"
  | "file_list"
  | "";

export const WIDGETS_BY_KIND: Record<Exclude<FieldKind, "" | "file_list">, readonly string[]> = {
  text: ["input", "textarea"],
  text_long: ["textarea", "input"],
  enum: ["select", "radio"],
  number: ["number"],
  boolean: ["checkbox", "switch"],
  file: ["file"],
  string_list: ["list", "tags"],
  enum_list: ["multiselect", "list"],
  number_list: ["list"],
  object: ["group"],
  object_list: ["repeater"],
};

const ITEM_KINDS: Partial<Record<FieldKind, FieldKind>> = {
  text: "string_list",
  text_long: "string_list",
  enum: "enum_list",
  number: "number_list",
  object: "object_list",
  file: "file_list",
};

export function fieldKind(schema: JsonSchema | undefined | null): FieldKind {
  if (!schema || typeof schema !== "object" || Object.keys(schema).length === 0) return "";
  if (schema["x-fs-type"] === "file") return "file";
  if (schema.writeOnly || schema["x-fs-type"] === "secret") return "";
  if (typeof schema.type !== "string") return "";
  if (schema.type === "string") {
    if (Array.isArray(schema.enum) && schema.enum.length) return "enum";
    return schema.format === "textarea" ? "text_long" : "text";
  }
  if (schema.type === "number" || schema.type === "integer") return "number";
  if (schema.type === "boolean") return "boolean";
  if (schema.type === "object") {
    return schema.properties && typeof schema.properties === "object" && Object.keys(schema.properties).length
      ? "object"
      : "";
  }
  if (schema.type === "array") return ITEM_KINDS[fieldKind(schema.items)] ?? "";
  return "";
}

export function isRenderable(kind: FieldKind): kind is Exclude<FieldKind, "" | "file_list"> {
  return kind !== "" && kind !== "file_list";
}

export function resolveSchemaPath(schema: JsonSchema | undefined, path: string): JsonSchema | undefined {
  let node: JsonSchema | undefined = schema;
  for (const segment of path.split(".")) {
    const intoItems = segment.endsWith("[]");
    const name = intoItems ? segment.slice(0, -2) : segment;
    const next = node?.properties?.[name];
    if (!next) return undefined;
    node = intoItems ? next.items : next;
  }
  return node;
}

export type InputFieldModel = {
  path: string;
  name: string;
  label: string;
  helpText: string;
  kind: Exclude<FieldKind, "" | "file_list">;
  widget: string;
  required: boolean;
  schema: JsonSchema;
};

function topLevelNames(schema: JsonSchema): string[] {
  return Object.keys(schema.properties ?? {});
}

/**
 * The form's fields in order: the section's configuration when it lists fields,
 * otherwise every top-level input the contract describes. Types, requirements
 * and options always come from the contract.
 */
export function buildInputFields(schema: JsonSchema, section?: Pick<ProjectUserSection, "fields">): InputFieldModel[] {
  const required = new Set(schema.required ?? []);
  const configured: SectionFieldConfig[] = section?.fields?.length
    ? [...section.fields].sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
    : topLevelNames(schema).map((name, index) => ({ path: name, order: index }));
  const fields: InputFieldModel[] = [];
  for (const config of configured) {
    if (config.visible === false || config.path.includes(".")) continue;
    const node = resolveSchemaPath(schema, config.path);
    const kind = fieldKind(node);
    if (!node || !isRenderable(kind)) continue;
    const allowed = WIDGETS_BY_KIND[kind];
    fields.push({
      path: config.path,
      name: config.path,
      label: config.label || node.title || config.path,
      helpText: config.help_text || (typeof node.description === "string" ? node.description : ""),
      kind,
      widget: config.widget && allowed.includes(config.widget) ? config.widget : allowed[0],
      required: required.has(config.path),
      schema: node,
    });
  }
  return fields;
}

/** Inputs the form cannot render although the contract requires them. */
export function unsupportedRequiredInputs(schema: JsonSchema): string[] {
  return (schema.required ?? []).filter((name) => !isRenderable(fieldKind(resolveSchemaPath(schema, name))));
}

/** Declared top-level outputs: the only output columns (ADR 0075). */
export function outputNames(schema: JsonSchema): string[] {
  return topLevelNames(schema);
}

/** Every input path a column may show: top level and described nested values. */
export function inputPaths(schema: JsonSchema, prefix = "", depth = 0): string[] {
  if (depth > 3) return [];
  const paths: string[] = [];
  for (const [name, node] of Object.entries(schema.properties ?? {})) {
    const kind = fieldKind(node);
    if (!kind) continue;
    const path = prefix ? `${prefix}.${name}` : name;
    paths.push(path);
    if (kind === "object") paths.push(...inputPaths(node, path, depth + 1));
    if (kind === "object_list" && node.items) paths.push(...inputPaths(node.items, `${path}[]`, depth + 1));
  }
  return paths;
}
