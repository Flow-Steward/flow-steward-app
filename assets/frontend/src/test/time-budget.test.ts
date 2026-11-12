import { describe, expect, it } from "vitest";

import {
  ASYNC_WAIT_BASE_MS,
  TEST_TIMEOUT_BASE_MS,
  asyncWaitMs,
  measureTestTimeScale,
  testTimeScale,
  testTimeoutMs,
  testWorkerCount,
  timeReferenceWork,
} from "./time-budget";

describe("test time budget", () => {
  it("never shortens the limits and caps how far they stretch", () => {
    expect(testTimeScale("")).toBe(1);
    expect(testTimeScale("not a number")).toBe(1);
    expect(testTimeScale("0")).toBe(1);
    expect(testTimeScale("0.4")).toBe(1);
    expect(testTimeScale("2.26")).toBe(2.3);
    expect(testTimeScale("50")).toBe(6);
  });

  it("stretches both hang guards by the same scale", () => {
    expect(asyncWaitMs(1)).toBe(ASYNC_WAIT_BASE_MS);
    expect(testTimeoutMs(1)).toBe(TEST_TIMEOUT_BASE_MS);
    expect(asyncWaitMs(2.5)).toBe(ASYNC_WAIT_BASE_MS * 2.5);
    expect(testTimeoutMs(2.5)).toBe(TEST_TIMEOUT_BASE_MS * 2.5);
    expect(asyncWaitMs(1)).toBeLessThan(testTimeoutMs(1));
  });

  it("scales by the busier of load per core and slow raw speed", () => {
    expect(measureTestTimeScale({ cores: 10, loadPerMinute: 0, workMs: 10 })).toBe(1);
    expect(measureTestTimeScale({ cores: 10, loadPerMinute: 20, workMs: 10 })).toBe(3);
    expect(measureTestTimeScale({ cores: 4, loadPerMinute: 0, workMs: 50 })).toBe(2);
    expect(measureTestTimeScale({ cores: 0, loadPerMinute: Number.NaN, workMs: Number.NaN })).toBe(1);
  });

  it("counts the run's own workers as load, since they start after the measurement", () => {
    expect(measureTestTimeScale({ cores: 10, loadPerMinute: 2, workMs: 10, workers: 4 })).toBe(1.6);
    expect(measureTestTimeScale({ cores: 4, loadPerMinute: 0, workMs: 10, workers: 4 })).toBe(2);
    expect(measureTestTimeScale({ cores: 10, loadPerMinute: 2, workMs: 10, workers: Number.NaN })).toBe(1.2);
  });

  it("times a fixed piece of work with the given clock", () => {
    const ticks = [100, 142];
    expect(timeReferenceWork(() => ticks.shift() ?? 0)).toBe(42);
  });

  it("runs four workers, never more than the cores, unless told otherwise", () => {
    expect(testWorkerCount(10)).toBe(4);
    expect(testWorkerCount(4)).toBe(4);
    expect(testWorkerCount(2)).toBe(2);
    expect(testWorkerCount(0)).toBe(1);
    expect(testWorkerCount(10, "6")).toBe(6);
    expect(testWorkerCount(10, "zero")).toBe(4);
  });
});
