import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { vi } from "vitest";

import {
  templatePreviewBody,
  templateSegments,
} from "@/pages/workflows/detail/detail-template-core";
import {
  TemplateResultPreview,
  TemplateTestRunResult,
} from "@/pages/workflows/detail/detail-template-preview";
import {
  TemplateStepSetup,
  TemplateTextEditor,
} from "@/pages/workflows/detail/detail-template-step";

function ControlledEditor({ initial, inputNames = ["header", "body"] }: { initial: string; inputNames?: string[] }) {
  const [value, setValue] = useState(initial);
  return (
    <>
      <TemplateTextEditor ariaLabel="Template text" inputNames={inputNames} onChange={setValue} value={value} />
      <output data-testid="value">{value}</output>
    </>
  );
}

function currentValue() {
  return screen.getByTestId("value").textContent;
}

describe("template highlighting", () => {
  it("marks complete expressions and tags and leaves everything else plain", () => {
    expect(templateSegments('Hi {{ name }}, {% if vip %}VIP{% endif %} {"a": 1}')).toEqual([
      { text: "Hi ", kind: "plain" },
      { text: "{{ name }}", kind: "expression" },
      { text: ", ", kind: "plain" },
      { text: "{% if vip %}", kind: "tag" },
      { text: "VIP", kind: "plain" },
      { text: "{% endif %}", kind: "tag" },
      { text: ' {"a": 1}', kind: "plain" },
    ]);
  });

  it("keeps an unfinished expression and a raw block as plain text", () => {
    expect(templateSegments("a {{ unfinished")).toEqual([{ text: "a {{ unfinished", kind: "plain" }]);
    expect(templateSegments("{% raw %}{{ literal }}{% endraw %}")).toEqual([
      { text: "{% raw %}{{ literal }}{% endraw %}", kind: "plain" },
    ]);
    expect(templateSegments("{% raw %}{{ open")).toEqual([
      { text: "{% raw %}{{ open", kind: "plain" },
    ]);
  });
});

describe("TemplateTextEditor", () => {
  it("types braces and JSON exactly as typed, without inserting anything", async () => {
    const user = userEvent.setup();
    render(<ControlledEditor initial="" />);

    await user.type(screen.getByLabelText("Template text"), '{{{{ header }} {{"a": [[1]}');

    expect(currentValue()).toBe('{{ header }} {"a": [1]}');
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("colours a finished expression, and editing or deleting it is plain text editing", async () => {
    const user = userEvent.setup();
    render(<ControlledEditor initial="Hi {{ header }}!" />);

    expect(screen.getByText("{{ header }}")).toHaveAttribute("data-template-expression", "true");
    const field = screen.getByLabelText("Template text");
    await user.clear(field);
    await user.type(field, "Hi {{{{ head");

    expect(currentValue()).toBe("Hi {{ head");
    expect(screen.queryByText("{{ header }}")).not.toBeInTheDocument();
    expect(screen.getAllByText("Hi {{ head")[0]).not.toHaveAttribute("data-template-expression");
  });

  it("inserts the chosen input at the caret as ordinary text", async () => {
    const user = userEvent.setup();
    render(<ControlledEditor initial="Hello  world" />);
    const field = screen.getByLabelText<HTMLTextAreaElement>("Template text");
    field.setSelectionRange(6, 6);

    await user.selectOptions(screen.getByLabelText("Insert variable"), "body");

    expect(currentValue()).toBe("Hello {{ body }} world");
    expect(screen.getByLabelText<HTMLSelectElement>("Insert variable").value).toBe("");
  });

  it("keeps leading spaces, blank lines and the trailing newline", () => {
    render(<ControlledEditor initial={"  a\n\n\nb\n"} />);

    expect(screen.getByLabelText<HTMLTextAreaElement>("Template text").value).toBe("  a\n\n\nb\n");
  });

  it("disables insertion until an input is declared", () => {
    render(<ControlledEditor initial="" inputNames={[]} />);

    expect(screen.getByLabelText("Insert variable")).toBeDisabled();
  });
});

describe("TemplateStepSetup", () => {
  function mountSetup(config: Record<string, unknown>) {
    const replaceStepKindConfigField = vi.fn();
    render(
      <TemplateStepSetup
        ctx={{
          renderWorkflowVariableInput: ({ ariaLabel, onChange, value }: { ariaLabel: string; onChange: (v: string) => void; value: string }) => (
            <input aria-label={ariaLabel} onChange={(event) => onChange(event.target.value)} value={value} />
          ),
          renderWorkflowDestinationInput: ({ ariaLabel, onChange, value }: { ariaLabel: string; onChange: (v: string) => void; value: string }) => (
            <input aria-label={ariaLabel} onChange={(event) => onChange(event.target.value)} value={value} />
          ),
          replaceStepKindConfigField,
          stepKindConfig: config,
        }}
        inputClass=""
      />,
    );
    return replaceStepKindConfigField;
  }

  it("adds, edits and removes declared inputs in the step configuration", async () => {
    const user = userEvent.setup();
    const replace = mountSetup({
      text: "{{ header }}",
      inputs: [{ name: "header", type: "text", required: true, source: "state.outputs.title" }],
    });

    await user.click(screen.getByRole("button", { name: "Add input" }));
    expect(replace).toHaveBeenLastCalledWith("template", "inputs", [
      { name: "header", type: "text", required: true, source: "state.outputs.title" },
      { name: "", type: "text", required: true, source: "" },
    ]);

    await user.selectOptions(screen.getByLabelText("Input 1 type"), "array");
    expect(replace).toHaveBeenLastCalledWith("template", "inputs", [
      { name: "header", type: "array", required: true, source: "state.outputs.title" },
    ]);

    fireEvent.change(screen.getByLabelText("Input 1 source"), { target: { value: "state.outputs.h" } });
    expect(replace).toHaveBeenLastCalledWith("template", "inputs", [
      { name: "header", type: "text", required: true, source: "state.outputs.h" },
    ]);

    await user.click(screen.getByRole("button", { name: "Remove input 1" }));
    expect(replace).toHaveBeenLastCalledWith("template", "inputs", []);
  });

  it("changes the format without touching the template text", async () => {
    const user = userEvent.setup();
    const replace = mountSetup({ text: "# {{ header }}\n", format: "markdown" });

    await user.selectOptions(screen.getByLabelText("Template format"), "html");

    expect(replace).toHaveBeenCalledTimes(1);
    expect(replace).toHaveBeenCalledWith("template", "format", "html");
    expect(screen.getByLabelText<HTMLTextAreaElement>("Template text").value).toBe("# {{ header }}\n");
  });

  it("links the Jinja documentation from the help", () => {
    mountSetup({ text: "" });

    expect(screen.getByRole("link", { name: "Jinja template documentation" })).toHaveAttribute(
      "href",
      "https://jinja.palletsprojects.com/en/stable/templates/",
    );
  });
});

describe("template result preview", () => {
  it("shows text results literally, with no preview", () => {
    render(<TemplateResultPreview format="text" stale={false} text="<b>not bold</b>" />);

    expect(screen.getByText("<b>not bold</b>")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Preview" })).not.toBeInTheDocument();
  });

  it("previews Markdown in the server's preview page and hands it the HTML once it is ready", async () => {
    const user = userEvent.setup();
    render(<TemplateResultPreview format="markdown" stale={false} text={"# Title\n\n| a |\n|---|\n| 1 |"} />);

    expect(screen.getByText(/# Title/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Preview" }));
    const frame = screen.getByTitle("Template preview") as HTMLIFrameElement;
    // Its own page, not srcdoc: a srcdoc frame inherits the admin policy, which
    // refuses every image that is not on this server.
    expect(frame).toHaveAttribute("src", "/app/template-preview-frame");
    // Sandboxed by the page's own response header, not by an attribute.
    expect(frame).not.toHaveAttribute("srcdoc");

    const frameWindow = frame.contentWindow as Window;
    const post = vi.spyOn(frameWindow, "postMessage").mockImplementation(() => undefined);
    // A message from any other window is not the page saying it is ready.
    act(() => {
      globalThis.dispatchEvent(
        new MessageEvent("message", { data: { type: "flow-steward:template-preview-ready" }, source: window }),
      );
    });
    expect(post).not.toHaveBeenCalled();

    act(() => {
      globalThis.dispatchEvent(
        new MessageEvent("message", { data: { type: "flow-steward:template-preview-ready" }, origin: "null", source: frameWindow }),
      );
    });
    expect(post).toHaveBeenCalledTimes(1);

    // The same message from a page with a real origin is not the sandboxed preview.
    act(() => {
      globalThis.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "flow-steward:template-preview-ready" },
          origin: "https://example.test",
          source: frameWindow,
        }),
      );
    });
    expect(post).toHaveBeenCalledTimes(1);
    const [message] = post.mock.calls[0] as [{ type: string; html: string }];
    expect(message.type).toBe("flow-steward:template-preview");
    expect(message.html).toContain("<h1>Title</h1>");
    expect(message.html).toContain("<table>");
  });

  it("prints raw HTML inside Markdown as text and passes an HTML template as it is", () => {
    const markdownBody = templatePreviewBody("Hi <script>alert(1)</script>", "markdown");
    expect(markdownBody).not.toContain("<script>");
    expect(markdownBody).toContain("&lt;script&gt;");

    const image = '<img alt="logo" src="https://example.test/logo.png">';
    expect(templatePreviewBody(image, "html")).toBe(image);
    expect(templatePreviewBody("![logo](https://example.test/logo.png)", "markdown")).toContain(
      'src="https://example.test/logo.png"',
    );
  });

  it("marks the result as old once the template or the test inputs change", () => {
    const result = { template: { ok: true, format: "html", output: "<p>Hi</p>" } };
    const { rerender } = render(<TemplateTestRunResult currentKey="v1" result={result} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    rerender(<TemplateTestRunResult currentKey="v2" result={result} />);
    expect(screen.getByRole("status")).toHaveTextContent("previous test");

    rerender(<TemplateTestRunResult currentKey="v2" result={{ ...result }} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("still marks the result as old after leaving Test Run to edit the step and coming back", () => {
    // Going to Smart setup unmounts the result; the same result must not come
    // back looking current once the template changed there.
    const result = { template: { ok: true, format: "markdown", output: "# Markdown result" } };
    const { unmount } = render(<TemplateTestRunResult currentKey="template v1" result={result} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    unmount();

    render(<TemplateTestRunResult currentKey="template v2 (UPDATED)" result={result} />);
    expect(screen.getByRole("status")).toHaveTextContent("previous test");
  });

  it("renders nothing until a test produced text", () => {
    const { container } = render(<TemplateTestRunResult currentKey="v1" result={{ template: { ok: false } }} />);

    expect(container).toBeEmptyDOMElement();
  });
});
