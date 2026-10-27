export function stringFromUnknownValue(value: unknown, fallback: string | number = ""): string {
  const fallbackText = typeof fallback === "string" ? fallback : "";
  if (typeof value === "string") {
    return value;
  }
  if (
    typeof value === "number" ||
    typeof value === "boolean" ||
    typeof value === "bigint"
  ) {
    return String(value);
  }
  return fallbackText;
}

export function firstStringFromUnknownValue(values: readonly unknown[], fallback = ""): string {
  for (const value of values) {
    const text = stringFromUnknownValue(value).trim();
    if (text) {
      return text;
    }
  }
  return fallback;
}

export function isTruthy<T>(
  value: T | false | "" | 0 | null | undefined,
): value is T {
  return Boolean(value);
}
