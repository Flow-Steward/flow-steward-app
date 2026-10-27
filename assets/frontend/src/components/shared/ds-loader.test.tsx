import { Search } from "lucide-react";
import { IconButton } from "@/components/ui/icon-button";
import type * as LoaderAdapter from "./ds-loader";
import { act, fireEvent, render, renderHook, screen, within } from "@testing-library/react";
import { afterEach, vi } from "vitest";

import { BrandLoader } from "@/components/shared/brand-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";

const adapters = import.meta.glob<typeof LoaderAdapter>("./ds-loader.tsx");

afterEach(() => { vi.useRealTimers(); });

describe("published loader lifecycle", () => {
  it("delays 400ms and keeps a shown loader for 500ms after a short request", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(({ pending }) => useLoaderDelay(pending), { initialProps: { pending: true } });
    expect(result.current).toBe(false);
    act(() => { vi.advanceTimersByTime(399); });
    expect(result.current).toBe(false);
    act(() => { vi.advanceTimersByTime(1); });
    expect(result.current).toBe(true);
    rerender({ pending: false });
    act(() => { vi.advanceTimersByTime(499); });
    expect(result.current).toBe(true);
    act(() => { vi.advanceTimersByTime(1); });
    expect(result.current).toBe(false);
  });

  it("never flashes for a request completing before the delay and resets for the next request", () => {
    vi.useFakeTimers();
    const { result, rerender, unmount } = renderHook(({ pending }) => useLoaderDelay(pending), { initialProps: { pending: true } });
    act(() => { vi.advanceTimersByTime(200); });
    rerender({ pending: false });
    act(() => { vi.advanceTimersByTime(600); });
    expect(result.current).toBe(false);
    rerender({ pending: true });
    act(() => { vi.advanceTimersByTime(399); });
    expect(result.current).toBe(false);
    act(() => { vi.advanceTimersByTime(1); });
    expect(result.current).toBe(true);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("maps all four variants to the published assets and announces only an explicit label", async () => {
    expect(adapters["./ds-loader.tsx"], "published loader adapter").toBeDefined();
    const { DsLoader } = await adapters["./ds-loader.tsx"]();
    const { rerender } = render(<DsLoader variant="wide-page" label="Preparing workflow" />);
    expect(screen.getByRole("status")).toHaveTextContent("Preparing workflow");
    expect(screen.getByTestId("loader-mark")).toHaveClass("fs-loader", "lg");
    expect(screen.getByTestId("loader-mark")).toHaveAttribute("aria-hidden", "true");
    rerender(<DsLoader variant="wide-block" />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByTestId("loader-mark")).toHaveClass("fs-loader");
    expect(screen.getByTestId("loader-mark")).not.toHaveClass("lg");
    rerender(<DsLoader variant="orbit-slot" />);
    expect(screen.getByTestId("loader-mark")).toHaveClass("fs-loader-orbit", "md");
    rerender(<DsLoader variant="orbit-inline" />);
    expect(screen.getByTestId("loader-mark")).toHaveClass("fs-loader-orbit");
    expect(screen.getByTestId("loader-mark")).not.toHaveClass("md");
  });

  it("preserves BrandLoader as the decorative wide block compatibility adapter", () => {
    render(<BrandLoader />);
    expect(screen.getByTestId("loader-mark")).toHaveClass("fs-loader");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});


describe("automatic IconButton loader lifecycle", () => {
  it("guards repeat requests immediately while its delayed glyph is still absent", () => {
    vi.useFakeTimers();
    const action = vi.fn();
    render(<IconButton aria-busy icon={Search} label="Run workflow" onClick={action} />);
    const button = screen.getByRole("button", { name: "Run workflow" });
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(within(button).queryByTestId("loader-mark")).not.toBeInTheDocument();
    fireEvent.click(button);
    expect(action).not.toHaveBeenCalled();
  });

  it("never paints a loader for an IconButton request completed before 400ms", () => {
    vi.useFakeTimers();
    const { rerender } = render(<IconButton aria-busy icon={Search} label="Run workflow" />);
    const button = screen.getByRole("button", { name: "Run workflow" });
    act(() => { vi.advanceTimersByTime(200); });
    rerender(<IconButton aria-busy={false} icon={Search} label="Run workflow" />);
    act(() => { vi.advanceTimersByTime(700); });
    expect(within(button).queryByTestId("loader-mark")).not.toBeInTheDocument();
    expect(button).toHaveAttribute("aria-busy", "false");
    expect(button).toHaveAccessibleName("Run workflow");
  });

  it("keeps a shown inline orbit for 500ms while request semantics become truthful immediately", () => {
    vi.useFakeTimers();
    const action = vi.fn();
    const { rerender } = render(<IconButton aria-busy icon={Search} label="Run workflow" onClick={action} />);
    const button = screen.getByRole("button", { name: "Run workflow" });
    act(() => { vi.advanceTimersByTime(400); });
    expect(within(button).getByTestId("loader-mark")).toHaveClass("fs-loader-orbit");
    expect(within(button).getByTestId("loader-mark")).not.toHaveClass("md");
    act(() => { vi.advanceTimersByTime(1); });
    rerender(<IconButton aria-busy={false} icon={Search} label="Run workflow" onClick={action} />);
    expect(button).toHaveAttribute("aria-busy", "false");
    expect(button).not.toHaveAttribute("aria-disabled", "true");
    fireEvent.click(button);
    expect(action).toHaveBeenCalledTimes(1);
    act(() => { vi.advanceTimersByTime(498); });
    expect(within(button).getByTestId("loader-mark")).toHaveAttribute("aria-hidden", "true");
    expect(button).toHaveAccessibleName("Run workflow");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(1); });
    expect(within(button).queryByTestId("loader-mark")).not.toBeInTheDocument();
  });
});
