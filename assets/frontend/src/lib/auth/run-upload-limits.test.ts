import { describe, expect, it } from "vitest";

import { exceedsRunUploadLimits } from "@/lib/auth/run-upload-limits";

const MIB = 1024 * 1024;
const policy = { max_file_bytes: 5 * MIB, max_total_bytes: 5 * MIB, max_files: 3 };

describe("exceedsRunUploadLimits", () => {
  it("accepts a phone photo larger than 1 MiB under the deployment policy", () => {
    expect(exceedsRunUploadLimits([3 * MIB], policy)).toBe(false);
  });

  it("refuses a file, a total or a count over the policy", () => {
    expect(exceedsRunUploadLimits([5 * MIB + 1], policy)).toBe(true);
    expect(exceedsRunUploadLimits([3 * MIB, 3 * MIB], policy)).toBe(true);
    expect(exceedsRunUploadLimits([1, 1, 1, 1], policy)).toBe(true);
  });

  it("treats zero as no limit and unknown limits as nothing to check here", () => {
    const unlimited = { max_file_bytes: 0, max_total_bytes: 0, max_files: 0 };
    expect(exceedsRunUploadLimits(new Array(50).fill(100 * MIB), unlimited)).toBe(false);
    expect(exceedsRunUploadLimits([100 * MIB], undefined)).toBe(false);
  });
});
