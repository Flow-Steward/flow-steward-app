import { fireEvent, render, screen } from "@testing-library/react";

import { I18nContext, fallbackContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { KeyValueEditor } from "@/pages/registry/mcp/detail/mcp-detail-page-components";
import type { KeyValueRow } from "@/pages/registry/mcp/key-value-rows";

describe("MCP key-value locale rendering", () => {
  it("localizes row validation without changing header keys or edited values", () => {
    const messages = { "react.registry.key_value_pair_required": "Eilutėse „{label}“ būtina nurodyti raktą ir reikšmę." };
    let changedRows: KeyValueRow[] = [];
    render(
      <I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, fallback = "", params) => interpolate(messages[key as keyof typeof messages] || fallback || key, params) }}>
        <KeyValueEditor addLabel="Pridėti antraštę" keyAriaLabel="Antraštės raktas" label="Antraštės" onChange={(rows) => { changedRows = rows; }} rows={[{ id: 1, key: "X-Tenant", value: "" }]} valueAriaLabel="Antraštės reikšmė" wrapperRef={{ current: null }} />
      </I18nContext.Provider>,
    );
    expect(screen.getByText("Eilutėse „Antraštės“ būtina nurodyti raktą ir reikšmę.")).toBeInTheDocument();
    const key = screen.getByRole("textbox", { name: "Antraštės raktas" });
    expect(key).toHaveValue("X-Tenant");
    expect(key).toHaveAttribute("aria-invalid", "true");
    fireEvent.change(screen.getByRole("textbox", { name: "Antraštės reikšmė" }), { target: { value: "operator-ru-value" } });
    expect(changedRows).toEqual([{ id: 1, key: "X-Tenant", value: "operator-ru-value" }]);
  });
});
