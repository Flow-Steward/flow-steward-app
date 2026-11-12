import "@testing-library/jest-dom/vitest";

import { configure } from "@testing-library/react";
import { beforeEach } from "vitest";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { asyncWaitMs } from "./time-budget";

function createTestStorage(): Storage {
  const values = new Map<string, string>();

  return {
    get length() {
      return values.size;
    },
    clear() {
      values.clear();
    },
    getItem(key: string) {
      return values.get(stringFromUnknownValue(key)) ?? null;
    },
    key(index: number) {
      return [...values.keys()][index] ?? null;
    },
    removeItem(key: string) {
      values.delete(stringFromUnknownValue(key));
    },
    setItem(key: string, value: string) {
      values.set(stringFromUnknownValue(key), stringFromUnknownValue(value));
    },
  };
}

// Every findBy*/waitFor ends as soon as its condition holds; this limit only calls a wait hung.
// The default (1 s) failed tests on busy or slower machines, never on a defect.
configure({ asyncUtilTimeout: asyncWaitMs() });

const testLocalStorage = createTestStorage();

if (globalThis.window !== undefined) {
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: testLocalStorage,
  });
}

Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  value: testLocalStorage,
});

// jsdom ships neither of these real browser APIs, so surfaces that legitimately use them
// would throw during render. Stub them here rather than guarding product code.
if (globalThis.window !== undefined) {
  // jsdom has no layout engine and may not implement point hit testing. Keep the fallback
  // on Document.prototype so canvas tests that temporarily define an own document method
  // can restore/delete that override and reveal this stable baseline.
  if (typeof Document !== "undefined" && typeof Document.prototype.elementsFromPoint !== "function") {
    Object.defineProperty(Document.prototype, "elementsFromPoint", {
      configurable: true,
      writable: true,
      value: () => [],
    });
  }
  if (typeof Element.prototype.scrollIntoView !== "function") {
    Element.prototype.scrollIntoView = function scrollIntoView() {};
  }
  if (typeof HTMLDialogElement !== "undefined") {
    if (typeof HTMLDialogElement.prototype.showModal !== "function") {
      HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
        this.open = true;
      };
    }
    if (typeof HTMLDialogElement.prototype.close !== "function") {
      HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
        this.open = false;
        this.dispatchEvent(new Event("close"));
      };
    }
  }
}

beforeEach(() => {
  testLocalStorage.clear();
});
