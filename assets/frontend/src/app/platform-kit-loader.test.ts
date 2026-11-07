import { loadPlatformKitInBackground, PLATFORM_KIT_URL } from "@/app/platform-kit-loader";

describe("Platform Kit loads after the app has rendered", () => {
  it("imports nothing until the scheduled moment, then imports the kit once", () => {
    const tasks: Array<() => void> = [];
    const importModule = vi.fn(() => Promise.resolve({}));
    loadPlatformKitInBackground({ importModule, schedule: (task) => tasks.push(task) });
    expect(importModule).not.toHaveBeenCalled();
    tasks.forEach((task) => task());
    expect(importModule).toHaveBeenCalledTimes(1);
    expect(importModule).toHaveBeenCalledWith(PLATFORM_KIT_URL);
  });

  it("only warns when the kit fails to load; the app keeps running", async () => {
    const warn = vi.fn();
    const failure = new Error("offline");
    loadPlatformKitInBackground({ importModule: () => Promise.reject(failure), schedule: (task) => task(), warn });
    await vi.waitFor(() => expect(warn).toHaveBeenCalledWith(expect.stringContaining("Platform kit failed to load"), failure));
  });
});
