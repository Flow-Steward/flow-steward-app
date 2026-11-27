import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { fetchJson } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";
import {
  normalizeStatusDocumentSurface,
  type StatusDocumentSurface,
} from "@/pages/settings/hub/status-document";
import { StatusDocumentPanel } from "@/pages/settings/hub/status-document-panel";

vi.mock("@/lib/http/client", () => ({
  fetchJson: vi.fn(),
}));

const mockedFetchJson = vi.mocked(fetchJson);

const STATUS_PATH = "/api/modules/acme.status/status";
const UPLOAD_PATH = "/api/modules/acme.status/document";

function declaration(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    kind: "status_document",
    status_path: STATUS_PATH,
    upload_path: UPLOAD_PATH,
    fields: [
      { key: "state", label: "State", type: "badge" },
      { key: "holder", label: "Holder", type: "text" },
      { key: "seats", label: "Seats" },
    ],
    upload_label: "Document text",
    accept: ".txt,.json",
    max_bytes: 64,
    ...overrides,
  };
}

function surfaceOf(overrides: Record<string, unknown> = {}): StatusDocumentSurface {
  const surface = normalizeStatusDocumentSurface(declaration(overrides));
  if (!surface) throw new Error("the fixture declaration must be valid");
  return surface;
}

function renderPanel(surface: StatusDocumentSurface = surfaceOf()) {
  return render(
    <MemoryRouter>
      <StatusDocumentPanel surface={surface} title="Module status" />
    </MemoryRouter>,
  );
}

function definition(label: string): HTMLElement {
  const terms = screen.getAllByRole("term");
  const index = terms.findIndex((term) => term.textContent === label);
  const value = screen.getAllByRole("definition")[index];
  if (index < 0 || !value) throw new Error(`no value for ${label}`);
  return value;
}

describe("normalizeStatusDocumentSurface", () => {
  it("reads the declaration with its defaults", () => {
    const surface = normalizeStatusDocumentSurface(declaration({ accept: undefined, max_bytes: undefined }));

    expect(surface).toMatchObject({
      kind: "status_document",
      statusPath: STATUS_PATH,
      uploadPath: UPLOAD_PATH,
      uploadField: "document",
      statusKey: "",
      accept: "",
      maxBytes: 65_536,
    });
    expect(surface?.fields.map((field) => field.type)).toEqual(["badge", "text", "text"]);
  });

  it.each([
    { status_path: "https://evil.example/api/modules/acme.status/status" },
    { status_path: "//evil.example/api/x" },
    { status_path: "/api/modules/acme.status/status?all=1" },
    { upload_path: "/api/modules/acme.status/{id}" },
    { upload_path: "/settings/document" },
    { fields: [] },
    { fields: [{ key: "a b", label: "A" }] },
    { upload_field: "document.text" },
    { status_key: "a.b" },
    { kind: "resource_report" },
  ])("refuses an unsafe or incomplete declaration %#", (overrides) => {
    expect(normalizeStatusDocumentSurface(declaration(overrides))).toBeUndefined();
  });
});

describe("StatusDocumentPanel", () => {
  beforeEach(() => {
    mockedFetchJson.mockReset();
  });

  it("shows the declared status fields as label and value rows", async () => {
    mockedFetchJson.mockResolvedValue({ state: "active", holder: "Example Ltd", seats: 25, ignored: "x" });

    renderPanel();

    await waitFor(() => expect(definition("State")).toHaveTextContent("active"));
    expect(definition("Holder")).toHaveTextContent("Example Ltd");
    expect(definition("Seats")).toHaveTextContent("25");
    expect(screen.queryByText("ignored")).not.toBeInTheDocument();
    expect(mockedFetchJson).toHaveBeenCalledWith(STATUS_PATH);
  });

  it("reads the status object under status_key and marks a missing value", async () => {
    mockedFetchJson.mockResolvedValue({ status: { state: "pending" } });

    renderPanel(surfaceOf({ status_key: "status" }));

    await waitFor(() => expect(definition("State")).toHaveTextContent("pending"));
    expect(definition("Holder")).toHaveTextContent("—");
  });

  it("says when there is no status yet", async () => {
    mockedFetchJson.mockResolvedValue({ status: null });

    renderPanel(surfaceOf({ status_key: "status", empty_text: "Nothing uploaded yet." }));

    await waitFor(() => expect(screen.getByText("Nothing uploaded yet.")).toBeInTheDocument());
  });

  it("puts the pasted document to the module and reads the status again", async () => {
    mockedFetchJson.mockImplementation(async (_input, init) => {
      if (init?.method === "PUT") return { ok: true };
      return { state: mockedFetchJson.mock.calls.some(([, call]) => call?.method === "PUT") ? "active" : "missing" };
    });

    renderPanel();
    await waitFor(() => expect(definition("State")).toHaveTextContent("missing"));

    fireEvent.change(screen.getByLabelText("Document text"), { target: { value: "signed-text" } });
    fireEvent.click(screen.getByRole("button", { name: "Upload" }));

    await waitFor(() =>
      expect(mockedFetchJson).toHaveBeenCalledWith(UPLOAD_PATH, {
        method: "PUT",
        body: JSON.stringify({ document: "signed-text" }),
      }),
    );
    await waitFor(() => expect(screen.getByText("Document uploaded.")).toBeInTheDocument());
    await waitFor(() => expect(definition("State")).toHaveTextContent("active"));
    expect(screen.getByLabelText("Document text")).toHaveValue("");
  });

  it("sends the document under the declared upload_field", async () => {
    mockedFetchJson.mockResolvedValue({ state: "active" });

    renderPanel(surfaceOf({ upload_field: "content" }));
    await waitFor(() => expect(definition("State")).toHaveTextContent("active"));
    fireEvent.change(screen.getByLabelText("Document text"), { target: { value: "abc" } });
    fireEvent.click(screen.getByRole("button", { name: "Upload" }));

    await waitFor(() =>
      expect(mockedFetchJson).toHaveBeenCalledWith(UPLOAD_PATH, { method: "PUT", body: JSON.stringify({ content: "abc" }) }),
    );
  });

  it("fills the text from a chosen file and refuses one over the limit", async () => {
    mockedFetchJson.mockResolvedValue({ state: "active" });
    renderPanel();
    await waitFor(() => expect(definition("State")).toHaveTextContent("active"));
    const input = screen.getByLabelText<HTMLInputElement>("Choose file");
    expect(input).toHaveAttribute("accept", ".txt,.json");

    fireEvent.change(input, { target: { files: [new File(["from-file"], "doc.txt", { type: "text/plain" })] } });
    await waitFor(() => expect(screen.getByLabelText("Document text")).toHaveValue("from-file"));

    fireEvent.change(input, { target: { files: [new File(["x".repeat(65)], "big.txt", { type: "text/plain" })] } });
    await waitFor(() => expect(screen.getByText("The document is larger than 64 B.")).toBeInTheDocument());
    expect(screen.getByLabelText("Document text")).toHaveValue("from-file");
  });

  it("refuses an empty or oversized document without a request", async () => {
    mockedFetchJson.mockResolvedValue({ state: "active" });
    renderPanel();
    await waitFor(() => expect(definition("State")).toHaveTextContent("active"));

    fireEvent.click(screen.getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(screen.getByText("Paste the document or choose a file first.")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Document text"), { target: { value: "y".repeat(65) } });
    fireEvent.click(screen.getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(screen.getByText("The document is larger than 64 B.")).toBeInTheDocument());
    expect(mockedFetchJson).toHaveBeenCalledTimes(1);
  });

  it("shows the module's refusal of a document", async () => {
    mockedFetchJson.mockImplementation(async (_input, init) => {
      if (init?.method === "PUT") {
        throw new HttpError({ status: 422, url: UPLOAD_PATH, payload: { message: "Signature does not match" } });
      }
      return { state: "active" };
    });
    renderPanel();
    await waitFor(() => expect(definition("State")).toHaveTextContent("active"));

    fireEvent.change(screen.getByLabelText("Document text"), { target: { value: "bad" } });
    fireEvent.click(screen.getByRole("button", { name: "Upload" }));

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.queryByText("Document uploaded.")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Document text")).toHaveValue("bad");
  });

  it("shows the page-unavailable state when the feature is not entitled", async () => {
    mockedFetchJson.mockRejectedValue(
      new HttpError({ status: 403, url: STATUS_PATH, payload: { code: "feature_not_entitled", message: "Not entitled" } }),
    );

    renderPanel();

    await waitFor(() => expect(screen.getByText("This page is not available.")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Upload" })).not.toBeInTheDocument();
  });
});
