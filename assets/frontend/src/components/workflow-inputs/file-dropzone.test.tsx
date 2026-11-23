/* eslint-disable testing-library/no-node-access -- the zone and the list are addressed by their own classes, as the design names them. */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { vi } from "vitest";

import { FileDropzone, useUploadLimitsHint } from "./file-dropzone";
import dropzoneStyles from "./file-dropzone.css?raw";

const limits = vi.hoisted(() => ({ value: undefined as undefined | { max_file_bytes: number; max_total_bytes: number; max_files: number } }));
vi.mock("@/lib/auth/run-upload-limits", () => ({ useRunUploadLimits: () => limits.value }));

function file(name: string, size: number, type = "application/pdf"): File {
  return new File([new Uint8Array(size)], name, { type, lastModified: 1 });
}

function Harness({ multiple = true, initial = [] as File[] }: Readonly<{ multiple?: boolean; initial?: File[] }>) {
  const [files, setFiles] = useState<File[]>(initial);
  const hint = useUploadLimitsHint(multiple);
  return <FileDropzone ariaLabel="Invoices" files={files} hint={hint} listLabel="Chosen invoices" multiple={multiple} onFiles={setFiles} />;
}

function input(): HTMLInputElement {
  return screen.getByLabelText("Invoices") as HTMLInputElement;
}

describe("FileDropzone", () => {
  afterEach(() => {
    limits.value = undefined;
  });

  it("asks to drag or browse and names the server's limits", () => {
    limits.value = { max_file_bytes: 25 * 1024 * 1024, max_total_bytes: 0, max_files: 10 };
    render(<Harness />);
    expect(screen.getByText(/Drag files here or/)).toBeInTheDocument();
    expect(input()).toHaveAttribute("type", "file");
    expect(input()).toHaveAttribute("multiple");
    // The input is hidden inside the browse link, so a click on the link opens the picker.
    expect(input().closest("label")).toHaveTextContent("browse");
    expect(screen.getByText("Up to 25 MB per file · up to 10 files")).toBeInTheDocument();
  });

  it("adds picked files without duplicates, then removes one or all", () => {
    render(<Harness />);
    fireEvent.change(input(), { target: { files: [file("a.pdf", 1024), file("b.png", 2048, "image/png")] } });
    fireEvent.change(input(), { target: { files: [file("a.pdf", 1024)] } });

    const list = screen.getByRole("list", { name: "Chosen invoices" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText("2 files · 3.0 KB")).toBeInTheDocument();
    expect(screen.getByText(/Add more: drag files or/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Remove a.pdf" }));
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByText("1 file · 2.0 KB")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Remove all" }));
    expect(screen.queryByRole("list", { name: "Chosen invoices" })).not.toBeInTheDocument();
  });

  it("takes dropped files and lights up while they hover", () => {
    const { container } = render(<Harness />);
    const zone = container.querySelector(".fs-dropzone") as HTMLElement;
    const dataTransfer = { types: ["Files"], files: [file("scan.pdf", 512)] };

    fireEvent.dragOver(zone, { dataTransfer });
    expect(zone).toHaveAttribute("data-over", "true");
    expect(screen.getByText(/Drop to add the files/)).toBeInTheDocument();
    // Only the drop invitation shows; the browse link stays for the keyboard.
    expect(zone.querySelector(".fs-dropzone-pick")).toHaveAttribute("data-hidden", "true");

    fireEvent.drop(zone, { dataTransfer });
    expect(zone).not.toHaveAttribute("data-over");
    expect(screen.getByRole("listitem")).toHaveTextContent("scan.pdf");
  });

  it("keeps one file in a single-file field: a new pick replaces it", () => {
    render(<Harness multiple={false} />);
    expect(screen.getByText(/Drag a file here or/)).toBeInTheDocument();
    fireEvent.change(input(), { target: { files: [file("first.pdf", 10)] } });
    fireEvent.change(input(), { target: { files: [file("second.pdf", 10)] } });
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByRole("listitem")).toHaveTextContent("second.pdf");
    expect(screen.getByText(/Replace it: drag a file or/)).toBeInTheDocument();
    // One file needs no count and no Remove all: its row has its own remove button.
    expect(screen.queryByRole("button", { name: "Remove all" })).not.toBeInTheDocument();
  });

  it("keeps the extension of a long name in view", () => {
    const name = "Paper_Co_terms-and-conditions-of-supply_rev-7.pdf";
    render(<Harness initial={[file(name, 10)]} />);
    const shown = screen.getByTitle(name);
    expect(shown.querySelector(".fs-dropzone-file-tail")).toHaveTextContent("supply_rev-7.pdf".slice(-12));
    expect(dropzoneStyles).toMatch(/\.fs-dropzone-file-head\s*\{[^}]*text-overflow:\s*ellipsis;/);
    expect(dropzoneStyles).toMatch(/\.fs-dropzone-file-tail\s*\{\s*flex:\s*none;/);
    // A space where the name is cut does not collapse.
    expect(dropzoneStyles).toMatch(/\.fs-dropzone-file-name\s*\{[^}]*white-space:\s*pre;/);
  });
});
