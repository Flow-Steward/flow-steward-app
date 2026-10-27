import { expect, it } from "vitest";

import { isSecondaryNodeAction } from "./canvas-core";

const point = { clientX: 12, clientY: 24 };

it("supports point hit testing when jsdom has no layout hit-test API", () => {
  expect(isSecondaryNodeAction(document.createElement("div"), point)).toBe(false);
});

it("reveals the prototype fallback after restoring a document-local mock", () => {
  const fallback = Document.prototype.elementsFromPoint;
  const ownDescriptor = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
  const secondary = document.createElement("button");

  try {
    Object.defineProperty(document, "elementsFromPoint", {
      configurable: true,
      value: () => [secondary],
    });
    expect(isSecondaryNodeAction(document.createElement("div"), point)).toBe(true);
  } finally {
    if (ownDescriptor) {
      Object.defineProperty(document, "elementsFromPoint", ownDescriptor);
    } else {
      Reflect.deleteProperty(document, "elementsFromPoint");
    }
  }

  expect(Object.getOwnPropertyDescriptor(document, "elementsFromPoint")).toBeUndefined();
  expect(document.elementsFromPoint).toBe(fallback);
  expect(isSecondaryNodeAction(document.createElement("div"), point)).toBe(false);
});
