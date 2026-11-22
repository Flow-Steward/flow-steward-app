/** How much of a spreadsheet the preview draws (ADR 0080). */

export const SPREADSHEET_PREVIEW_ROWS = 200;
export const SPREADSHEET_PREVIEW_COLUMNS = 50;

/**
 * The first `limit` CSV records and how many there are in all. A line break
 * inside a quoted field belongs to its record, as in the CSV the file holds;
 * a doubled quote inside a field toggles twice and changes nothing.
 */
export function csvPreviewSlice(text: string, limit: number): { text: string; total: number } {
  let quoted = false;
  let breaks = 0;
  let cut = text.length;
  let index = 0;
  while (index < text.length) {
    const code = text.charCodeAt(index);
    if (code === 34) {
      quoted = !quoted;
    } else if (!quoted && (code === 10 || code === 13)) {
      if (code === 13 && text.charCodeAt(index + 1) === 10) index += 1;
      breaks += 1;
      if (breaks === limit) cut = index + 1;
    }
    index += 1;
  }
  const lastEnd = text.length && /[\r\n]$/.test(text) ? 0 : 1;
  const total = text.length ? breaks + lastEnd : 0;
  return { text: total > limit ? text.slice(0, cut) : text, total };
}
