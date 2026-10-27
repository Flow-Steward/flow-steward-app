import { describe, expect, it } from "vitest";

import {
  isBasicEmailAddress,
  trimRepeatedEdges,
  trimRepeatedEnd,
  trimRepeatedStart,
} from "@/lib/utils/string-normalization";
import { replaceDelimitedTokens } from "@/lib/utils/template-tokens";

describe("bounded string normalization", () => {
  it("trims only the requested repeated edge character", () => {
    expect(trimRepeatedStart("///app/", "/")).toBe("app/");
    expect(trimRepeatedEnd("///app///", "/")).toBe("///app");
    expect(trimRepeatedEdges("___field___", "_")).toBe("field");
  });

  it("replaces complete delimited tokens without consuming incomplete input", () => {
    expect(replaceDelimitedTokens("Hello {name}!", "{", "}", (token) => token.toUpperCase()))
      .toBe("Hello NAME!");
    expect(replaceDelimitedTokens("Hello {name", "{", "}", (token) => token.toUpperCase()))
      .toBe("Hello {name");
  });

  it("recognizes the basic email shape used by OAuth hints", () => {
    expect(isBasicEmailAddress("user@example.test")).toBe(true);
    expect(isBasicEmailAddress("user@@example.test")).toBe(false);
    expect(isBasicEmailAddress("user@example")).toBe(false);
    expect(isBasicEmailAddress("user @example.test")).toBe(false);
  });
});
