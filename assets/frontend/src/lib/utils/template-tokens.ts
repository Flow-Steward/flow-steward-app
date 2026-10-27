export function replaceDelimitedTokens(
  value: string,
  opening: string,
  closing: string,
  replace: (token: string) => string,
): string {
  if (!opening || !closing) {
    throw new TypeError("template delimiters must not be empty");
  }
  const parts: string[] = [];
  let cursor = 0;
  while (cursor < value.length) {
    const openingIndex = value.indexOf(opening, cursor);
    if (openingIndex < 0) {
      parts.push(value.slice(cursor));
      break;
    }
    const tokenStart = openingIndex + opening.length;
    const closingIndex = value.indexOf(closing, tokenStart);
    if (closingIndex < 0) {
      parts.push(value.slice(cursor));
      break;
    }
    parts.push(
      value.slice(cursor, openingIndex),
      replace(value.slice(tokenStart, closingIndex)),
    );
    cursor = closingIndex + closing.length;
  }
  return parts.join("");
}
