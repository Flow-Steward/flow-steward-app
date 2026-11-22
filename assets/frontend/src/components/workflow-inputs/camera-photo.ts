/**
 * A photo taken with the phone's camera from a form, before it is sent.
 *
 * Phones name every shot the same ("image.jpg") and take 3–8 MB pictures of a
 * receipt that reads just as well at a quarter of that. A shot gets its own
 * name with the time it was taken, and a large one is scaled down to
 * {@link PHOTO_MAX_EDGE} pixels on its long side as a JPEG. Anything the
 * browser cannot decode or shrink (HEIC outside Safari, an old browser) is sent
 * as it was taken, only renamed. Files chosen from the gallery or the file
 * system are never touched: they may be originals someone has to keep.
 */

/** Long side of a scaled-down photo, in pixels: small print stays readable. */
export const PHOTO_MAX_EDGE = 2560;
/** JPEG quality of a scaled-down photo. */
export const PHOTO_QUALITY = 0.85;
/** A photo at or below this size is only renamed. */
export const PHOTO_SHRINK_ABOVE_BYTES = 1.5 * 1024 * 1024;

const EXTENSIONS: Record<string, string> = {
  "image/jpeg": "jpg",
  "image/png": "png",
  "image/webp": "webp",
  "image/heic": "heic",
  "image/heif": "heif",
};

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/** "photo-20261120-101503.jpg": when it was taken, in the device's own time. */
export function cameraPhotoName(taken: Date, extension: string): string {
  const day = `${taken.getFullYear()}${pad(taken.getMonth() + 1)}${pad(taken.getDate())}`;
  const time = `${pad(taken.getHours())}${pad(taken.getMinutes())}${pad(taken.getSeconds())}`;
  return `photo-${day}-${time}.${extension}`;
}

function extensionOf(file: File): string {
  const fromName = /\.([a-z0-9]+)$/i.exec(file.name)?.[1]?.toLowerCase();
  return EXTENSIONS[file.type] ?? fromName ?? "jpg";
}

function renamed(file: File, taken: Date): File {
  return new File([file], cameraPhotoName(taken, extensionOf(file)), { type: file.type, lastModified: taken.getTime() });
}

function encodeJpeg(canvas: HTMLCanvasElement): Promise<Blob | null> {
  return new Promise((resolve) => {
    canvas.toBlob(resolve, "image/jpeg", PHOTO_QUALITY);
  });
}

/** The shot as it is sent: renamed, and scaled down when it is large. */
export async function prepareCameraPhoto(file: File, taken: Date = new Date()): Promise<File> {
  if (!file.type.startsWith("image/") || typeof globalThis.createImageBitmap !== "function") return renamed(file, taken);
  let bitmap: ImageBitmap | null = null;
  try {
    bitmap = await globalThis.createImageBitmap(file, { imageOrientation: "from-image" });
    const scale = Math.min(1, PHOTO_MAX_EDGE / Math.max(bitmap.width, bitmap.height));
    if (scale === 1 && file.size <= PHOTO_SHRINK_ABOVE_BYTES) return renamed(file, taken);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const context = canvas.getContext("2d");
    if (!context) return renamed(file, taken);
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await encodeJpeg(canvas);
    // Never send a "smaller" photo that came out larger.
    if (!blob || blob.size >= file.size) return renamed(file, taken);
    return new File([blob], cameraPhotoName(taken, "jpg"), { type: "image/jpeg", lastModified: taken.getTime() });
  } catch {
    return renamed(file, taken);
  } finally {
    bitmap?.close();
  }
}
