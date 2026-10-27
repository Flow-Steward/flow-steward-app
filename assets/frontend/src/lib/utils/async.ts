export function ignoreAsyncResult(result: unknown): void {
  Promise.resolve(result).catch(() => undefined);
}
