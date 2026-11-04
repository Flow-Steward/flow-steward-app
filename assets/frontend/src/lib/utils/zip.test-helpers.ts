import { crc32 } from "@/lib/utils/zip";

/** Read a stored-only zip by its central directory, checking each entry's CRC. */
export function readStoredZip(archive: Uint8Array): Map<string, string> {
  const view = new DataView(archive.buffer, archive.byteOffset, archive.byteLength);
  const end = archive.byteLength - 22;
  if (view.getUint32(end, true) !== 0x06054b50) throw new Error("no end of central directory");
  const count = view.getUint16(end + 10, true);
  let position = view.getUint32(end + 16, true);
  const decoder = new TextDecoder();
  const files = new Map<string, string>();
  for (let index = 0; index < count; index += 1) {
    if (view.getUint32(position, true) !== 0x02014b50) throw new Error("bad central entry");
    const crc = view.getUint32(position + 16, true);
    const size = view.getUint32(position + 20, true);
    const nameLength = view.getUint16(position + 28, true);
    const local = view.getUint32(position + 42, true);
    const name = decoder.decode(archive.subarray(position + 46, position + 46 + nameLength));
    if (view.getUint32(local, true) !== 0x04034b50) throw new Error("bad local header");
    const dataStart = local + 30 + view.getUint16(local + 26, true) + view.getUint16(local + 28, true);
    const data = archive.subarray(dataStart, dataStart + size);
    if (crc32(data) !== crc) throw new Error(`CRC mismatch for ${name}`);
    files.set(name, decoder.decode(data));
    position += 46 + nameLength;
  }
  return files;
}

export async function blobBytes(blob: Blob): Promise<Uint8Array> {
  if (typeof blob.arrayBuffer === "function") return new Uint8Array(await blob.arrayBuffer());
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsArrayBuffer(blob);
  });
}
