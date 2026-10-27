import type {
  ExternalResourcePickerBridge,
  ExternalResourcePickerHostDeclaration,
} from "@/components/extensions/external-resource-picker";

type ExternalResourcePickerBridgeDependencies = {
  executeBootstrap: (actionId: string) => Promise<unknown>;
  globalObject?: Record<string, unknown>;
  loadScript?: (source: string) => Promise<void>;
  resolveAssetScript?: (assetRef: string) => string;
};

type BrowserPickerBridge = {
  open: (value: {
    bootstrap: Record<string, unknown>;
    options: Record<string, unknown>;
  }) => Promise<unknown>;
};

const externalScriptLoads = new Map<string, Promise<void>>();
const EXTERNAL_PICKER_TIMEOUT_MS = 10_000;
const PICKER_FIELD_ID = /^[A-Za-z_][A-Za-z0-9_-]{0,127}$/u;
const PICKER_ACTION_ID = /^[a-z][a-z0-9_]{0,63}$/u;
const PICKER_BRIDGE_PATH = /^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*){0,4}$/u;
const NONCANONICAL_RAW_URL_CHARACTERS = /["'<>^`{}]/u;
const PICKER_ASSET_REF = /^assets\/(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_-]+\.js$/u;
const PICKER_DATA_KEYS = new Set([
  "allowed_connect_origins",
  "allowed_frame_origins",
  "allowed_script_origins",
  "bootstrap_action_id",
  "bridge_global",
  "busy_label",
  "button_label",
  "error_message",
  "options",
  "scripts",
  "selection",
  "target_component_id",
]);
const PICKER_SELECTION_KEYS = new Set(["fields", "maximum"]);
const PICKER_FIELD_KEYS = new Set(["max_length", "required", "source", "target"]);
const CANONICAL_HTTPS_HOST = /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/u;
const FORBIDDEN_BOOTSTRAP_KEYS = new Set([
  "accesstoken",
  "authorization",
  "bearertoken",
  "clientsecret",
  "oauthaccesstoken",
  "oauthclientsecret",
  "platformaccesstoken",
  "platformtoken",
  "refreshtoken",
  "sessiontoken",
  "secretrefs",
  "token",
]);

function hasOnlyKeys(value: object, allowed: ReadonlySet<string>): boolean {
  return Object.keys(value).every((key) => allowed.has(key));
}

function wellFormedUtf16Length(value: string): number | null {
  for (let index = 0; index < value.length; index += 1) {
    const codePoint = value.codePointAt(index) ?? 0;
    if (codePoint >= 0xd800 && codePoint <= 0xdfff) {
      return null;
    }
    if (codePoint > 0xffff) {
      index += 1;
    }
  }
  return value.length;
}

function jsonStringBudget(value: string): number | null {
  if (wellFormedUtf16Length(value) === null) {
    return null;
  }
  let budget = 2;
  for (let index = 0; index < value.length; index += 1) {
    const codePoint = value.codePointAt(index) ?? 0;
    if (codePoint > 0xffff) {
      budget += 2;
      index += 1;
    } else if (
      codePoint === 0x22
      || codePoint === 0x5c
      || codePoint === 0x08
      || codePoint === 0x0c
      || codePoint === 0x0a
      || codePoint === 0x0d
      || codePoint === 0x09
    ) {
      budget += 2;
    } else if (codePoint < 0x20) {
      budget += 6;
    } else {
      budget += 1;
    }
  }
  return budget;
}

const NOT_JSON_PRIMITIVE = Symbol("not-json-primitive");

function jsonPrimitiveBudget(value: unknown): number | null | typeof NOT_JSON_PRIMITIVE {
  if (value === null) return 4;
  if (typeof value === "boolean") return value ? 4 : 5;
  if (typeof value === "string") return jsonStringBudget(value);
  if (typeof value !== "number") return NOT_JSON_PRIMITIVE;
  if (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value))) return null;
  return 32;
}

function jsonContainerEntries(value: object): Array<[string | null, unknown]> | null {
  if (Array.isArray(value)) return value.map((item) => [null, item]);
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null ? Object.entries(value) : null;
}

function jsonEntriesBudget(
  entries: Array<[string | null, unknown]>,
  depth: number,
  seen: WeakSet<object>,
): number | null {
  const rows: number[] = [];
  for (const [key, item] of entries) {
    const itemBudget = jsonStructuralBudget(item, depth + 1, seen);
    const keyBudget = key === null ? 0 : jsonStringBudget(key);
    if (itemBudget === null || keyBudget === null) return null;
    rows.push(keyBudget + (key === null ? 0 : 1) + itemBudget);
  }
  return 2 + rows.reduce((total, row) => total + row, 0) + Math.max(0, rows.length - 1);
}

function jsonStructuralBudget(
  value: unknown,
  depth = 0,
  seen = new WeakSet<object>(),
): number | null {
  if (depth > 32) return null;
  const primitiveBudget = jsonPrimitiveBudget(value);
  if (primitiveBudget !== NOT_JSON_PRIMITIVE) return primitiveBudget;
  if (!value || typeof value !== "object") return null;
  const entries = jsonContainerEntries(value);
  if (!entries || seen.has(value)) return null;
  seen.add(value);
  const budget = jsonEntriesBudget(entries, depth, seen);
  seen.delete(value);
  return budget;
}

function validRequiredCopy(value: unknown, maximum: number): value is string {
  if (typeof value !== "string") {
    return false;
  }
  const length = wellFormedUtf16Length(value);
  return length !== null && length <= maximum && Boolean(value.trim());
}

function validOptionalCopy(value: unknown, maximum: number): boolean {
  return value === undefined || value === null || validRequiredCopy(value, maximum);
}

function validatePickerDeclaration(declaration: ExternalResourcePickerHostDeclaration) {
  const { fields, maximum } = declaration.selection || {};
  const targets = new Set<string>();
  if (
    !hasOnlyKeys(declaration, PICKER_DATA_KEYS)
    || !declaration.selection
    || !hasOnlyKeys(declaration.selection, PICKER_SELECTION_KEYS)
    || !validRequiredCopy(declaration.button_label, 120)
    || !validOptionalCopy(declaration.busy_label, 120)
    || !validOptionalCopy(declaration.error_message, 500)
    || typeof declaration.bootstrap_action_id !== "string"
    || !PICKER_ACTION_ID.test(declaration.bootstrap_action_id)
    || typeof declaration.bridge_global !== "string"
    || !PICKER_BRIDGE_PATH.test(declaration.bridge_global)
    || typeof declaration.target_component_id !== "string"
    || !PICKER_FIELD_ID.test(declaration.target_component_id)
    || !Number.isInteger(maximum)
    || maximum < 1
    || maximum !== 1
    || !Array.isArray(fields)
    || fields.length < 1
    || fields.length > 32
  ) {
    throw new Error("invalid_external_picker_declaration");
  }
  for (const field of fields) {
    if (
      !field
      || typeof field !== "object"
      || !hasOnlyKeys(field, PICKER_FIELD_KEYS)
      || typeof field.source !== "string"
      || !PICKER_FIELD_ID.test(field.source)
      || typeof field.target !== "string"
      || !PICKER_FIELD_ID.test(field.target)
      || (field.required !== undefined && typeof field.required !== "boolean")
      || !Number.isInteger(field.max_length)
      || field.max_length < 1
      || field.max_length > 4_096
      || targets.has(field.target)
    ) {
      throw new TypeError("invalid_external_picker_declaration");
    }
    targets.add(field.target);
  }
  if (declaration.options !== undefined && declaration.options !== null) {
    if (typeof declaration.options !== "object" || Array.isArray(declaration.options)) {
      throw new TypeError("invalid_external_picker_declaration");
    }
    const optionsBudget = jsonStructuralBudget(declaration.options);
    if (optionsBudget === null || optionsBudget > 32_768) {
      throw new Error("invalid_external_picker_declaration");
    }
  }
  validateOptionalOrigins(declaration.allowed_connect_origins);
  validateOptionalOrigins(declaration.allowed_frame_origins);
}

function canonicalHttpsOrigin(value: unknown): value is string {
  if (typeof value !== "string" || [...value].some((character) => {
    const code = character.codePointAt(0) ?? 0;
    return code <= 0x20 || code === 0x7f;
  })) {
    return false;
  }
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:"
      && parsed.origin === value
      && CANONICAL_HTTPS_HOST.test(parsed.hostname)
      && parsed.hostname === parsed.hostname.toLowerCase()
      && !parsed.username
      && !parsed.password;
  } catch {
    return false;
  }
}

function validateOptionalOrigins(origins: readonly string[] | undefined): void {
  if (origins === undefined) {
    return;
  }
  if (
    !Array.isArray(origins)
    || origins.length < 1
    || origins.length > 4
    || new Set(origins).size !== origins.length
    || origins.some((origin) => !canonicalHttpsOrigin(origin))
  ) {
    throw new Error("invalid_external_picker_declaration");
  }
}

function loadExternalScript(source: string): Promise<void> {
  const existing = externalScriptLoads.get(source);
  if (existing) {
    return existing;
  }
  const pending = new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    const timeout = globalThis.setTimeout(() => {
      script.remove();
      reject(new Error("external_picker_script_unavailable"));
    }, EXTERNAL_PICKER_TIMEOUT_MS);
    script.async = true;
    script.defer = true;
    script.src = source;
    script.addEventListener("load", () => {
      globalThis.clearTimeout(timeout);
      resolve();
    }, { once: true });
    script.addEventListener("error", () => {
      globalThis.clearTimeout(timeout);
      script.remove();
      reject(new Error("external_picker_script_unavailable"));
    }, { once: true });
    document.head.append(script);
  }).catch((error: unknown) => {
    externalScriptLoads.delete(source);
    throw error;
  });
  externalScriptLoads.set(source, pending);
  return pending;
}

function validatedScriptSources(
  declaration: ExternalResourcePickerHostDeclaration,
  resolveAssetScript?: (assetRef: string) => string,
): string[] {
  if (!Array.isArray(declaration.scripts) || !declaration.scripts.length || declaration.scripts.length > 4) {
    throw new Error("invalid_external_picker_script");
  }
  if (
    !Array.isArray(declaration.allowed_script_origins)
    || !declaration.allowed_script_origins.length
    || declaration.allowed_script_origins.length > 4
  ) {
    throw new Error("invalid_external_picker_script");
  }
  const origins = new Set(declaration.allowed_script_origins);
  if (
    origins.size !== declaration.allowed_script_origins.length
    || declaration.allowed_script_origins.some((origin) => !canonicalHttpsOrigin(origin))
  ) {
    throw new Error("invalid_external_picker_script");
  }
  const sources = declaration.scripts.map((script) => {
    if (!script || typeof script !== "object") {
      throw new Error("invalid_external_picker_script");
    }
    if (hasOnlyKeys(script, new Set(["asset_ref"])) && "asset_ref" in script) {
      const assetRef = script.asset_ref;
      if (
        typeof assetRef !== "string"
        || !PICKER_ASSET_REF.test(assetRef)
        || !resolveAssetScript
      ) {
        throw new Error("invalid_external_picker_script");
      }
      const resolved = resolveAssetScript(assetRef);
      if (typeof resolved !== "string" || !resolved.startsWith("/api/extensions/")) {
        throw new Error("invalid_external_picker_script");
      }
      return resolved;
    }
    if (!hasOnlyKeys(script, new Set(["src"])) || !("src" in script)) {
      throw new Error("invalid_external_picker_script");
    }
    const { src } = script;
    if (typeof src !== "string" || NONCANONICAL_RAW_URL_CHARACTERS.test(src)) {
      throw new Error("invalid_external_picker_script");
    }
    try {
      const parsed = new URL(src);
      if (
        src.length > 2048
        || parsed.toString() !== src
        || parsed.protocol !== "https:"
        || Boolean(parsed.username || parsed.password)
        || !origins.has(parsed.origin)
        || !src.startsWith(`${parsed.origin}/`)
      ) {
        throw new Error("invalid_external_picker_script");
      }
      return parsed.toString();
    } catch {
      throw new Error("invalid_external_picker_script");
    }
  });
  if (new Set(sources).size !== sources.length) {
    throw new Error("invalid_external_picker_script");
  }
  return sources;
}

function resolveBrowserBridge(
  source: Record<string, unknown>,
  path: string,
): BrowserPickerBridge {
  if (!PICKER_BRIDGE_PATH.test(path)) {
    throw new Error("invalid_external_picker_bridge");
  }
  let value: unknown = source;
  for (const segment of path.split(".")) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error("external_picker_bridge_unavailable");
    }
    value = (value as Record<string, unknown>)[segment];
  }
  if (!value || typeof value !== "object" || typeof (value as BrowserPickerBridge).open !== "function") {
    throw new Error("external_picker_bridge_unavailable");
  }
  return value as BrowserPickerBridge;
}

function boundedBootstrap(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("invalid_external_picker_bootstrap");
  }
  const encoded = JSON.stringify(value);
  if (!encoded || encoded.length > 32_768) {
    throw new Error("invalid_external_picker_bootstrap");
  }
  const containsForbiddenMaterial = (candidate: unknown): boolean => {
    if (Array.isArray(candidate)) {
      return candidate.some(containsForbiddenMaterial);
    }
    if (!candidate || typeof candidate !== "object") {
      return false;
    }
    return Object.entries(candidate).some(([key, nested]) => {
      const normalizedKey = key.toLowerCase().replace(/[^a-z0-9]/gu, "");
      return FORBIDDEN_BOOTSTRAP_KEYS.has(normalizedKey) || containsForbiddenMaterial(nested);
    });
  };
  if (containsForbiddenMaterial(value)) {
    throw new Error("invalid_external_picker_bootstrap");
  }
  return value as Record<string, unknown>;
}

export function createExternalResourcePickerBridge(
  declaration: ExternalResourcePickerHostDeclaration,
  dependencies: ExternalResourcePickerBridgeDependencies,
): ExternalResourcePickerBridge {
  validatePickerDeclaration(declaration);
  const sources = validatedScriptSources(declaration, dependencies.resolveAssetScript);
  const loadScript = dependencies.loadScript || loadExternalScript;
  const globalObject = dependencies.globalObject
    || globalThis as unknown as Record<string, unknown>;
  return {
    async open() {
      const [, bootstrap] = await Promise.all([
        Promise.all(sources.map((source) => loadScript(source))),
        dependencies.executeBootstrap(declaration.bootstrap_action_id),
      ]);
      const result = await resolveBrowserBridge(globalObject, declaration.bridge_global).open({
        bootstrap: boundedBootstrap(bootstrap),
        options: declaration.options || {},
      });
      if (!Array.isArray(result)) {
        throw new TypeError("invalid_external_picker_selection");
      }
      if (result.some((row) => !row || typeof row !== "object" || Array.isArray(row))) {
        throw new Error("invalid_external_picker_selection");
      }
      return result as Array<Record<string, unknown>>;
    },
  };
}
