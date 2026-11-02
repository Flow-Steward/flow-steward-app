/** Validate the runtime state-path grammar for file selectors (no wildcards). */
export function isAgentFileStatePath(value: string): boolean {
  const text = value.trim();
  if (!text.startsWith("state.") || text.length <= 6) return false;
  const path = text.slice(6);
  let index = 0;
  let expectSegment = true;
  while (index < path.length) {
    const char = path[index];
    if (char === ".") {
      if (expectSegment) return false;
      expectSegment = true;
      index += 1;
    } else if (char === "[") {
      let quote = ""; let escaped = false; let close = -1;
      for (let i = index + 1; i < path.length; i += 1) {
        const current = path[i];
        if (escaped) { escaped = false; continue; }
        if (current === "\\") { escaped = true; continue; }
        if (quote) {
          if (current === quote) { quote = ""; }
          continue;
        }
        if (current === "'" || current === '"') { quote = current; continue; }
        if (current === "]") { close = i; break; }
      }
      if (close < 0) return false;
      const raw = path.slice(index + 1, close).trim();
      if (raw.startsWith('"')) {
        try { const parsed: unknown = JSON.parse(raw); if (typeof parsed !== "string" || !parsed) return false; } catch { return false; }
      } else if (raw.startsWith("'")) {
        if (raw.length < 3 || !raw.endsWith("'")) return false;
      } else if (!/^\p{Decimal_Number}+$/u.test(raw)) return false;
      expectSegment = false; index = close + 1;
    } else if (char === "]") return false;
    else {
      const start = index;
      while (index < path.length && path[index] !== "." && path[index] !== "[") index += 1;
      const key = path.slice(start, index).trim();
      if (!key || /[?(){}]/.test(key)) return false;
      expectSegment = false;
    }
  }
  return !expectSegment;
}
