import { crc32, createZip } from "@/lib/utils/zip";
import { readStoredZip } from "@/lib/utils/zip.test-helpers";

describe("createZip", () => {
  it("computes the standard CRC-32", () => {
    expect(crc32(new TextEncoder().encode("123456789"))).toBe(0xcbf43926);
  });

  it("writes entries a zip reader finds by the central directory, UTF-8 names included", () => {
    const archive = createZip([
      { path: "folder/a.txt", content: "first\n" },
      { path: "folder/.hidden/b.json", content: '{"ok": true}\n' },
      { path: "ūnicode/č.md", content: "Ąžuolas" },
    ]);
    expect(Array.from(archive.subarray(0, 4))).toEqual([0x50, 0x4b, 0x03, 0x04]);
    expect(readStoredZip(archive)).toEqual(new Map([
      ["folder/a.txt", "first\n"],
      ["folder/.hidden/b.json", '{"ok": true}\n'],
      ["ūnicode/č.md", "Ąžuolas"],
    ]));
  });

  it("gives the same bytes for the same files", () => {
    const entries = [{ path: "a.txt", content: "same" }];
    expect(createZip(entries)).toEqual(createZip(entries));
  });

  it("refuses a path that leaves the archive or repeats", () => {
    expect(() => createZip([{ path: "../evil", content: "" }])).toThrow(/Invalid or repeated/);
    expect(() => createZip([{ path: "a", content: "" }, { path: "a", content: "" }])).toThrow(/Invalid or repeated/);
  });
});
