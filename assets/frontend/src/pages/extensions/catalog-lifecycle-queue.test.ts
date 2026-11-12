import { catalogLifecycleBusy, runCatalogLifecycle } from "./catalog-lifecycle-queue";
import { updateLabel } from "./marketplace-model";

function deferred() {
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<void>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

it("runs a second update only after the first ends, even when the first fails", async () => {
  const order: string[] = [];
  const first = deferred();
  expect(catalogLifecycleBusy()).toBe(false);
  const one = runCatalogLifecycle(async () => { order.push("first:start"); await first.promise; order.push("first:end"); });
  const two = runCatalogLifecycle(async () => { order.push("second"); return "updated"; });
  expect(catalogLifecycleBusy()).toBe(true);
  await Promise.resolve();
  expect(order).toEqual(["first:start"]);
  first.reject(new Error("Update failed."));
  await expect(one).rejects.toThrow("Update failed.");
  await expect(two).resolves.toBe("updated");
  expect(order).toEqual(["first:start", "second"]);
  await vi.waitFor(() => expect(catalogLifecycleBusy()).toBe(false));
});

it("names a queued update on its button", () => {
  expect(updateLabel("queued", "1.2.3")).toBe("Queued");
  expect(updateLabel("idle", "1.2.3")).toBe("Update to v1.2.3");
});
