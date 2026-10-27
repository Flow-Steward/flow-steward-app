export function normalizeFlashMessage(raw: string | null) {
  if (!raw) {
    return "";
  }
  const value = raw.replaceAll("+", " ").trim();
  if (!value) {
    return "";
  }
  return value.slice(0, 240);
}
