import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clipboardImageFile, releaseClipboardImage } from "./hotkeyRuntime";

const imageResource = vi.hoisted(() => ({
  close: vi.fn(async () => {}),
  rgba: vi.fn(async () => new Uint8Array([1, 2, 3, 255, 4, 5, 6, 255])),
  size: vi.fn(async () => ({ width: 2, height: 1 })),
  ids: [] as number[],
}));

vi.mock("@tauri-apps/api/image", () => ({
  Image: class {
    constructor(resourceId: number) {
      imageResource.ids.push(resourceId);
    }

    close = imageResource.close;
    rgba = imageResource.rgba;
    size = imageResource.size;
  },
}));

describe("clipboard images", () => {
  const putImageData = vi.fn();
  const canvas = {
    width: 0,
    height: 0,
    getContext: vi.fn(() => ({ putImageData })),
    toBlob: vi.fn((callback: BlobCallback) => {
      callback(new Blob(["png"], { type: "image/png" }));
    }),
  };

  beforeEach(() => {
    imageResource.close.mockClear();
    imageResource.rgba.mockClear();
    imageResource.size.mockClear();
    imageResource.ids.length = 0;
    putImageData.mockClear();
    canvas.getContext.mockClear();
    canvas.toBlob.mockClear();
    vi.spyOn(document, "createElement").mockReturnValue(canvas as unknown as HTMLCanvasElement);
    vi.stubGlobal(
      "ImageData",
      class {
        constructor(
          public data: Uint8ClampedArray,
          public width: number,
          public height: number,
        ) {}
      },
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("encodes an image resource as PNG and releases it", async () => {
    const file = await clipboardImageFile(27);

    expect(imageResource.ids).toEqual([27]);
    expect(canvas.width).toBe(2);
    expect(canvas.height).toBe(1);
    expect(putImageData).toHaveBeenCalledWith(
      expect.objectContaining({ width: 2, height: 1 }),
      0,
      0,
    );
    expect(file.name).toBe("clipboard.png");
    expect(file.type).toBe("image/png");
    expect(imageResource.close).toHaveBeenCalledOnce();
  });

  it("can release an image that arrived after the window closed", async () => {
    await releaseClipboardImage(51);

    expect(imageResource.ids).toEqual([51]);
    expect(imageResource.close).toHaveBeenCalledOnce();
  });
});
