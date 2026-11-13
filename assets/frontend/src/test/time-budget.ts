/**
 * How long a test may wait before it is called hung.
 *
 * A test must fail only on a defect, never on how fast the machine is. Every wait in a test
 * (`findBy*`, `waitFor`, `vi.waitFor`) ends as soon as its condition holds, so a passing test
 * costs the same on a fast and a slow machine; these limits only stop a test that would
 * otherwise hang. They are generous on purpose and stretch on slower or busier machines.
 *
 * `FS_TEST_TIME_SCALE` stretches them: the test config measures the machine when it starts and
 * sets it for every worker (see `measureTestTimeScale`), and a person may set it by hand.
 */
const MIN_SCALE = 1;
const MAX_SCALE = 6;

/** The scale every worker reads; 1 on a free, fast machine. */
export function testTimeScale(raw: string | undefined = readScaleFromEnvironment()): number {
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) return MIN_SCALE;
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, Math.round(value * 10) / 10));
}

function readScaleFromEnvironment(): string | undefined {
  return typeof process === "undefined" ? undefined : process.env.FS_TEST_TIME_SCALE;
}

/** The longest any single wait for the UI may take before the test is called hung. */
export const ASYNC_WAIT_BASE_MS = 10_000;
/**
 * The longest a whole test may take before it is called hung: the one test timeout every
 * test runs on; no test sets its own. The longest Builder flows take about 9 s alone and
 * four times that while the whole suite shares the machine, so 30 s was a speed check.
 */
export const TEST_TIMEOUT_BASE_MS = 60_000;

export function asyncWaitMs(scale = testTimeScale()): number {
  return ASYNC_WAIT_BASE_MS * scale;
}

export function testTimeoutMs(scale = testTimeScale()): number {
  return TEST_TIMEOUT_BASE_MS * scale;
}

/**
 * Measures how much slower this machine is right now than a free reference machine:
 * busy cores and raw speed (a fixed piece of work). Never below 1. The load is read before
 * the run starts, so the run's own workers are added to it: they are about to busy the cores.
 */
export function measureTestTimeScale({ cores, loadPerMinute, workMs, workers = 0, referenceWorkMs = 25 }: {
  cores: number;
  loadPerMinute: number;
  workMs: number;
  workers?: number;
  referenceWorkMs?: number;
}): number {
  const ownLoad = Number.isFinite(workers) ? Math.max(0, workers) : 0;
  const busy = cores > 0 && Number.isFinite(loadPerMinute) ? 1 + (Math.max(0, loadPerMinute) + ownLoad) / cores : 1;
  const slow = workMs > 0 && Number.isFinite(workMs) ? workMs / referenceWorkMs : 1;
  return testTimeScale(String(Math.max(busy, slow)));
}

/** A fixed piece of CPU work; how long it takes says how fast this machine is now. */
export function timeReferenceWork(now: () => number = () => performance.now()): number {
  const started = now();
  let checksum = 0;
  for (let index = 0; index < 2_000_000; index += 1) checksum = (checksum + Math.sqrt(index) * 31) % 1_000_003;
  const elapsed = now() - started;
  return checksum < 0 ? Number.NaN : elapsed;
}

/**
 * Parallel test workers: four, as the gate has always run, and never more than the machine's
 * cores, so a small machine is not swamped. `FS_TEST_WORKERS` overrides it.
 */
export function testWorkerCount(cores: number, override?: string): number {
  const forced = Number(override);
  if (Number.isInteger(forced) && forced > 0) return forced;
  return Math.max(1, Math.min(4, Math.floor(cores)));
}
