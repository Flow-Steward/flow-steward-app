export function trimRepeatedStart(value: string, character: string): string {
  if (character.length !== 1) {
    throw new TypeError("trim character must contain exactly one code unit");
  }
  let start = 0;
  while (value[start] === character) {
    start += 1;
  }
  return value.slice(start);
}

export function trimRepeatedEnd(value: string, character: string): string {
  if (character.length !== 1) {
    throw new TypeError("trim character must contain exactly one code unit");
  }
  let end = value.length;
  while (end > 0 && value[end - 1] === character) {
    end -= 1;
  }
  return value.slice(0, end);
}

export function trimRepeatedEdges(value: string, character: string): string {
  return trimRepeatedEnd(trimRepeatedStart(value, character), character);
}

export function isBasicEmailAddress(value: string): boolean {
  const [localPart, domain, extra] = value.split("@");
  return Boolean(
    localPart
    && domain
    && extra === undefined
    && !Array.from(value).some((character) => /\s/u.test(character))
    && domain.includes(".")
    && !domain.startsWith(".")
    && !domain.endsWith("."),
  );
}
