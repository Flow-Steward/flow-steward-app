import { afterEach, describe, expect, it, vi } from "vitest";

import { cameraPhotoName, PHOTO_MAX_EDGE, prepareCameraPhoto } from "./camera-photo";

const TAKEN = new Date(2026, 10, 20, 10, 15, 3);

function photo(bytes: number, name = "image.jpg", type = "image/jpeg"): File {
  return new File([new Uint8Array(bytes)], name, { type });
}

function fakeDecoder(width: number, height: number) {
  const close = vi.fn();
  vi.stubGlobal("createImageBitmap", vi.fn(async () => ({ width, height, close })));
  return close;
}

function fakeCanvas(encodedBytes: number) {
  const drawImage = vi.fn();
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage } as unknown as CanvasRenderingContext2D);
  const toBlob = vi
    .spyOn(HTMLCanvasElement.prototype, "toBlob")
    .mockImplementation(function (this: HTMLCanvasElement, callback: BlobCallback) {
      callback(new Blob([new Uint8Array(encodedBytes)], { type: "image/jpeg" }));
    });
  return { drawImage, toBlob };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("camera photos", () => {
  it("names every shot by the time it was taken", () => {
    expect(cameraPhotoName(TAKEN, "jpg")).toBe("photo-20261120-101503.jpg");
  });

  it("scales a large shot down to the long edge as a JPEG", async () => {
    const close = fakeDecoder(4000, 3000);
    const { drawImage } = fakeCanvas(400_000);

    const sent = await prepareCameraPhoto(photo(6 * 1024 * 1024), TAKEN);

    expect(sent.name).toBe("photo-20261120-101503.jpg");
    expect(sent.type).toBe("image/jpeg");
    expect(sent.size).toBe(400_000);
    expect(drawImage).toHaveBeenCalledWith(expect.anything(), 0, 0, PHOTO_MAX_EDGE, 1920);
    expect(close).toHaveBeenCalled();
  });

  it("only renames a small shot", async () => {
    fakeDecoder(1200, 900);
    const { toBlob } = fakeCanvas(10);

    const sent = await prepareCameraPhoto(photo(300_000), TAKEN);

    expect(sent.name).toBe("photo-20261120-101503.jpg");
    expect(sent.size).toBe(300_000);
    expect(toBlob).not.toHaveBeenCalled();
  });

  it("keeps the shot when scaling would not make it smaller", async () => {
    fakeDecoder(5000, 4000);
    fakeCanvas(3 * 1024 * 1024);

    const sent = await prepareCameraPhoto(photo(2 * 1024 * 1024, "IMG_0001.png", "image/png"), TAKEN);

    expect(sent.name).toBe("photo-20261120-101503.png");
    expect(sent.size).toBe(2 * 1024 * 1024);
  });

  it("sends a shot the browser cannot decode as it was taken, renamed", async () => {
    vi.stubGlobal("createImageBitmap", vi.fn(async () => Promise.reject(new Error("HEIC"))));

    const sent = await prepareCameraPhoto(photo(5 * 1024 * 1024, "image.heic", "image/heic"), TAKEN);

    expect(sent.name).toBe("photo-20261120-101503.heic");
    expect(sent.type).toBe("image/heic");
    expect(sent.size).toBe(5 * 1024 * 1024);
  });
});
