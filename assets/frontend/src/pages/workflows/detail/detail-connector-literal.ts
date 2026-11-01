/** Validation of known literal values against projected operation input contracts. */

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
export function connectorLiteralWidget(parameter: Record<string, unknown> | undefined): string {
  return String(parameter?.widget_type || parameter?.widget || parameter?.type || "").toLowerCase();
}
export function connectorStringLiteral(parameter: Record<string, unknown> | undefined): boolean {
  const type = String(parameter?.value_type || record(parameter?.schema).type || "");
  return type === "text" || type === "string" || (!type && ["text", "textarea"].includes(connectorLiteralWidget(parameter)));
}
type SchemaMatch = "match" | "mismatch" | "deferred";

/** Follow the host validator's Python equality for JSON values. */
function jsonEqual(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (typeof left === "boolean" && typeof right === "number") return Number(left) === right;
  if (typeof right === "boolean" && typeof left === "number") return left === Number(right);
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length && left.every((item, index) => jsonEqual(item, right[index]));
  }
  if (left && right && typeof left === "object" && typeof right === "object" && !Array.isArray(left) && !Array.isArray(right)) {
    const a = left as Record<string, unknown>;
    const b = right as Record<string, unknown>;
    const keys = Object.keys(a);
    return keys.length === Object.keys(b).length && keys.every((key) => Object.hasOwn(b, key) && jsonEqual(a[key], b[key]));
  }
  return false;
}

/** Patterns use Python re on the host; uncertain results must reach that authority. */
function schemaMatches(value: unknown, schema: Record<string, unknown>): SchemaMatch {
  let result: SchemaMatch = "match";
  const include = (match: SchemaMatch) => {
    if (match === "mismatch") result = "mismatch";
    else if (match === "deferred" && result === "match") result = "deferred";
  };
  const type = schema.type;
  if (Array.isArray(type) && !type.some((candidate) => typeof candidate === "string" && schemaMatches(value, { type: candidate }) === "match")) return "mismatch";
  if (typeof type === "string") {
    const match = type === "array" ? Array.isArray(value) : type === "object" ? value !== null && typeof value === "object" && !Array.isArray(value)
      : type === "integer" ? typeof value === "number" && Number.isInteger(value)
        : type === "null" ? value === null : typeof value === type;
    if (!match) return "mismatch";
  }
  if (Array.isArray(schema.enum) && !schema.enum.some((item) => jsonEqual(item, value))) return "mismatch";
  if (Object.hasOwn(schema, "const") && !jsonEqual(schema.const, value)) return "mismatch";
  if (typeof value === "string") {
    const length = [...value].length;
    if (typeof schema.minLength === "number" && length < schema.minLength) return "mismatch";
    if (typeof schema.maxLength === "number" && length > schema.maxLength) return "mismatch";
    if (typeof schema.pattern === "string") include("deferred");
  }
  if (typeof value === "number") {
    if (typeof schema.minimum === "number" && value < schema.minimum) return "mismatch";
    if (typeof schema.maximum === "number" && value > schema.maximum) return "mismatch";
  }
  if (Array.isArray(value)) {
    if (typeof schema.minItems === "number" && value.length < schema.minItems) return "mismatch";
    if (typeof schema.maxItems === "number" && value.length > schema.maxItems) return "mismatch";
    if (schema.items) value.forEach((item) => include(schemaMatches(item, record(schema.items))));
  }
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const object = value as Record<string, unknown>;
    const properties = record(schema.properties);
    if (Array.isArray(schema.required) && schema.required.some((key) => typeof key === "string" && !Object.hasOwn(object, key))) return "mismatch";
    for (const [key, item] of Object.entries(object)) {
      if (Object.hasOwn(properties, key)) include(schemaMatches(item, record(properties[key])));
      else if (schema.additionalProperties === false) return "mismatch";
      else if (typeof schema.additionalProperties === "object") include(schemaMatches(item, record(schema.additionalProperties)));
    }
  }
  if (Array.isArray(schema.allOf)) schema.allOf.forEach((item) => include(schemaMatches(value, record(item))));
  if (Array.isArray(schema.anyOf)) {
    const matches = schema.anyOf.map((item) => schemaMatches(value, record(item)));
    include(matches.includes("match") ? "match" : matches.includes("deferred") ? "deferred" : "mismatch");
  }
  if (Array.isArray(schema.oneOf)) {
    const matches = schema.oneOf.map((item) => schemaMatches(value, record(item)));
    const known = matches.filter((match) => match === "match").length;
    include(known > 1 ? "mismatch" : matches.includes("deferred") ? "deferred" : known === 1 ? "match" : "mismatch");
  }
  if (schema.not) {
    const match = schemaMatches(value, record(schema.not));
    include(match === "deferred" ? match : match === "match" ? "mismatch" : "match");
  }
  if (schema.if) {
    const condition = schemaMatches(value, record(schema.if));
    const branchMatch = (branch: unknown): SchemaMatch => branch ? schemaMatches(value, record(branch)) : "match";
    if (condition === "deferred") {
      const thenMatch = branchMatch(schema.then);
      const elseMatch = branchMatch(schema.else);
      include(thenMatch === elseMatch ? thenMatch : "deferred");
    } else include(branchMatch(condition === "match" ? schema.then : schema.else));
  }
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const object = value as Record<string, unknown>;
    for (const [key, required] of Object.entries(record(schema.dependentRequired))) {
      if (Object.hasOwn(object, key) && Array.isArray(required) && required.some((item) => typeof item === "string" && !Object.hasOwn(object, item))) return "mismatch";
    }
  }
  return result;
}
/** Match the backend's Python str.strip() before it parses a literal payload.
 * Python includes U+001C–U+001F and excludes U+FEFF, unlike JS trim(). */
export function isEncodedConnectorNull(value: unknown): boolean {
  if (typeof value !== "string" || !value.startsWith("literal:")) return false;
  const payload = value.slice("literal:".length);
  const isPythonWhitespace = (character: string) => {
    const code = character.charCodeAt(0);
    return (code >= 0x1c && code <= 0x1f) || /\p{White_Space}/u.test(character);
  };
  let start = 0;
  let end = payload.length;
  while (start < end && isPythonWhitespace(payload[start])) start += 1;
  while (end > start && isPythonWhitespace(payload[end - 1])) end -= 1;
  return payload.slice(start, end) === "null";
}
/** A declared schema, rather than the widget hint, determines whether null is valid. */
export function connectorAllowsNull(parameter: Record<string, unknown> | undefined): boolean {
  const schema = record(parameter?.schema);
  return connectorLiteralWidget(parameter) !== "artifact_picker"
    && Object.keys(schema).length > 0
    && schemaMatches(null, schema) !== "mismatch";
}
export type ConnectorLiteralError = "artifact" | "json" | "boolean" | "number" | "integer" | "schema" | "";
export function connectorLiteralError(parameter: Record<string, unknown> | undefined, text: string): ConnectorLiteralError {
  const widget = connectorLiteralWidget(parameter);
  if (widget === "artifact_picker") return "artifact";
  if (!connectorStringLiteral(parameter) && !text.trim()) return "";
  const schema = record(parameter?.schema);
  const integer = !connectorStringLiteral(parameter)
    && (widget === "integer" || parameter?.value_type === "integer" || schema.type === "integer")
    && !(Array.isArray(schema.type) && schema.type.includes("number"));
  let value: unknown = text;
  if (!connectorStringLiteral(parameter)) {
    try { value = JSON.parse(text); } catch {
      if (widget === "json") return "json";
      if (["number", "integer"].includes(widget)) return "number";
    }
  }
  if (widget === "boolean" && typeof value !== "boolean") return "boolean";
  if (["number", "integer"].includes(widget) && (typeof value !== "number" || !Number.isFinite(value))) return "number";
  if (integer && typeof value === "number" && !/^-?(?:0|[1-9][0-9]*)$/.test(text.trim())) return "integer";
  if (widget === "integer" && typeof value === "number" && !Number.isInteger(value)) return "integer";
  const declaredType = String(parameter?.value_type || "");
  const inferredType = declaredType === "text" ? "string" : ["string", "number", "integer", "boolean", "array", "object", "null"].includes(declaredType) ? declaredType : "";
  if (schemaMatches(value, { ...(inferredType ? { type: inferredType } : {}), ...schema }) === "mismatch") return "schema";
  return "";
}
export function invalidConnectorLiteralNames(mapping: unknown, parameters: Array<Record<string, unknown>>): string[] {
  const values = record(mapping);
  return parameters.flatMap((parameter) => {
    const name = String(parameter.name || parameter.key || parameter.id || parameter.field || "").trim();
    const value = values[name];
    if (typeof value !== "string" || !value.startsWith("literal:")) return [];
    let text = value.slice(8);
    if (isEncodedConnectorNull(value) && Object.keys(record(parameter.schema)).length > 0) {
      return connectorAllowsNull(parameter) ? [] : [name];
    }
    if (connectorStringLiteral(parameter)) {
      try { const decoded: unknown = JSON.parse(text); if (typeof decoded === "string") text = decoded; } catch { /* legacy plain text */ }
    }
    return connectorLiteralError(parameter, text) ? [name] : [];
  });
}
