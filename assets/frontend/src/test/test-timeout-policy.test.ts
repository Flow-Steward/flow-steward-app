import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

function testFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...testFiles(full));
    else if (/\.test\.tsx?$/.test(entry.name)) out.push(full);
  }
  return out;
}

// A test's own timeout as the trailing argument of it()/test(): `}, 15_000);`.
// Milliseconds only, so a closing call such as `hook({...}, 1);` is not one.
const OWN_TEST_TIMEOUT = /^\s*\}, \d[\d_]{3,}\);\s*$/m;

describe("test timeout policy", () => {
  it("leaves every test on the one timeout in vitest.config.ts", () => {
    // A per-test value shorter than the shared one failed heavy builder suites
    // on a loaded runner while the shared timeout would have let them finish.
    const offenders = testFiles("src").filter((file) => OWN_TEST_TIMEOUT.test(readFileSync(file, "utf8")));
    expect(offenders, "drop the timeout argument; vitest.config.ts testTimeout applies").toEqual([]);
  });
});
