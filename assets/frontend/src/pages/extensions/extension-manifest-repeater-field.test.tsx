import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { useAuth } from "@/lib/auth/useAuth";
import { I18nProvider } from "@/lib/i18n/I18nProvider";
import { ManifestRepeaterField } from "@/pages/extensions/extension-manifest-repeater-field";

vi.mock("@/lib/auth/useAuth", () => ({ useAuth: vi.fn(() => ({ session: null })) }));

const field = {
  field_id: "records",
  path: "records",
  label: "Records",
  props: {
    fields: [
      {
        path: "record_id",
        label: "Record ID",
        widget: "text",
        required: true,
        props: { immutable_on_edit: true },
      },
      { path: "active", label: "Active", widget: "boolean" },
    ],
  },
};

function RepeaterHarness() {
  const [value, setValue] = useState<unknown[]>([]);
  return (
    <ManifestRepeaterField
      componentId="batch_action"
      field={field}
      onChange={(_fieldName, nextValue) => setValue(nextValue as unknown[])}
      value={value}
    />
  );
}

describe("ManifestRepeaterField", () => {
  it("waits for delayed query values without claiming the initial empty state", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const savedRow = {
      record_id: "saved-record",
      active: true,
      _meta: {
        existing: true,
        immutable_fields: ["record_id"],
        original_record_id: "saved-record",
      },
    };
    const { rerender } = render(
      <ManifestRepeaterField
        componentId="batch_action"
        field={field}
        onChange={onChange}
        value={[]}
      />,
    );

    expect(screen.getByText("No rows yet.")).toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();

    rerender(
      <ManifestRepeaterField
        componentId="batch_action"
        field={field}
        onChange={onChange}
        value={[savedRow]}
      />,
    );

    const row = screen.getByTestId("manifest-repeater-row");
    expect(row).toHaveTextContent("saved-record");
    await user.click(within(row).getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("textbox", { name: /Record ID/ })).toBeDisabled();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("adds two independent rows and keeps the survivor identity when one is removed", async () => {
    const user = userEvent.setup();
    render(<RepeaterHarness />);

    await user.click(screen.getByRole("button", { name: "+ Add row" }));
    await user.type(screen.getByLabelText("Record ID"), "alpha");
    await user.click(screen.getByRole("button", { name: "Add row" }));

    await user.click(screen.getByRole("button", { name: "+ Add row" }));
    await user.type(screen.getByLabelText("Record ID"), "beta");
    await user.click(screen.getByRole("button", { name: "Add row" }));

    const rows = screen.getAllByTestId("manifest-repeater-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("alpha");
    expect(rows[1]).toHaveTextContent("beta");
    const betaKey = rows[1].getAttribute("data-row-key");

    await user.click(within(rows[0]).getByRole("button", { name: "Remove" }));

    const survivor = screen.getByTestId("manifest-repeater-row");
    expect(survivor).toHaveTextContent("beta");
    expect(survivor).toHaveAttribute("data-row-key", betaKey);
  });
});


describe("ManifestRepeaterField required marks", () => {
  it("marks a required row field the way every other required field is marked", async () => {
    const user = userEvent.setup();
    const markedField = {
      ...field,
      props: {
        fields: [...field.props.fields, { path: "note", label: "Note", widget: "text" }],
      },
    };
    render(<ManifestRepeaterField componentId="batch_action" field={markedField} onChange={vi.fn()} value={[]} />);
    await user.click(screen.getByRole("button", { name: "+ Add row" }));

    expect(screen.getByRole("textbox", { name: "Record ID" })).toBeRequired();
    expect(screen.getByRole("textbox", { name: "Note" })).not.toBeRequired();
    // One required field (Record ID), one mark; the optional text and the checkbox get none.
    expect(screen.getAllByText("*")).toHaveLength(1);
  });
});

describe("ManifestRepeaterField in the interface language", () => {
  // Extension labels arrive already translated from the extension's own
  // dictionary; the platform's controls and messages come from the UI catalog.
  const lithuanian = {
    "extensions.repeater.add_row_button": "+ Pridėti eilutę",
    "extensions.repeater.add_row": "Pridėti eilutę",
    "extensions.repeater.cancel": "Atšaukti",
    "extensions.repeater.empty": "Eilučių dar nėra.",
    "extensions.repeater.field_required": "Laukas „{field}“ privalomas.",
  };

  afterEach(() => {
    vi.mocked(useAuth).mockReturnValue({ session: null } as never);
  });

  it("states a missing required value wholly in the selected language", async () => {
    vi.mocked(useAuth).mockReturnValue({
      session: { i18n: { locale: "lt", fallback_locale: "en", supported_locales: [], messages: lithuanian } },
    } as never);
    const onChange = vi.fn();
    const user = userEvent.setup();
    const translatedField = {
      ...field,
      props: { fields: [{ ...field.props.fields[0], label: "Įrašo ID" }, field.props.fields[1]] },
    };
    render(
      <I18nProvider>
        <ManifestRepeaterField componentId="batch_action" field={translatedField} onChange={onChange} value={[]} />
      </I18nProvider>,
    );

    expect(screen.getByText("Eilučių dar nėra.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "+ Pridėti eilutę" }));
    expect(screen.getByRole("button", { name: "Atšaukti" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Pridėti eilutę" }));

    expect(screen.getByText("Laukas „Įrašo ID“ privalomas.")).toBeInTheDocument();
    expect(screen.queryByText(/is required/)).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("keeps the English text when the language has no catalog entry", async () => {
    vi.mocked(useAuth).mockReturnValue({
      session: { i18n: { locale: "ru", fallback_locale: "en", supported_locales: [], messages: {} } },
    } as never);
    const user = userEvent.setup();
    render(
      <I18nProvider>
        <ManifestRepeaterField componentId="batch_action" field={field} onChange={vi.fn()} value={[]} />
      </I18nProvider>,
    );

    await user.click(screen.getByRole("button", { name: "+ Add row" }));
    await user.click(screen.getByRole("button", { name: "Add row" }));
    expect(screen.getByText("Record ID is required.")).toBeInTheDocument();
  });
});
