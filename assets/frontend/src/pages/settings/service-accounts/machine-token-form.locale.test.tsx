import { render, screen } from "@testing-library/react";
import { createRef } from "react";

import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { MachineTokenForm } from "@/pages/settings/service-accounts/machine-token-form";

const messages: Record<string, string> = {
  "auth.common.optional": "(nebūtina)",
  "react.settings.machine_token_label": "Mašininio prieigos rakto žyma",
  "react.settings.token_expiry_future_day_hint": "Pasirinkite būsimą datą. Ankstyviausia leidžiama diena – rytojus.",
  "react.settings.token_expiry_date_days_hint": "Galioja iki {date}, po {days} dienų nuo šiandien.",
};

function form(date: string) {
  return <I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}><MachineTokenForm accountScopes={["jobs:read"]} message="" onCreate={() => undefined} onUpdate={() => undefined} setTokenExpiryDate={() => undefined} setTokenExpiryMode={() => undefined} setTokenExpiresError={() => undefined} setTokenLabelInput={() => undefined} setTokenScopesInput={() => undefined} timezone="UTC" tokenDrawerMode="create" tokenExpiryDate={date} tokenExpiryMode="custom" tokenExpiresError="" tokenExpiresRef={createRef<HTMLInputElement>()} tokenLabelInput="Fixture token" tokenScopesInput={["jobs:read"]} /></I18nContext.Provider>;
}

describe("Lithuanian custom token expiry guidance", () => {
  beforeEach(() => vi.useFakeTimers({ toFake: ["Date"] }).setSystemTime(new Date("2026-10-10T12:00:00Z")));
  afterEach(() => vi.useRealTimers());

  it("includes the localized optional marker in the machine token label accessible name", () => {
    render(form(""));
    const label = screen.getByRole("textbox", { name: "Mašininio prieigos rakto žyma (nebūtina)" });
    expect(label).toHaveValue("Fixture token");
    expect(label).not.toBeRequired();
    expect(screen.getByLabelText("Expires on")).toHaveAttribute("min", "2026-10-11");
  });

  it("localizes the future-day hint for an empty custom expiry", () => {
    render(form(""));
    expect(screen.getByText("Pasirinkite būsimą datą. Ankstyviausia leidžiama diena – rytojus.")).toBeInTheDocument();
    expect(screen.getByLabelText("Expires on")).toHaveAttribute("min", "2026-10-11");
  });

  it("localizes a chosen future expiry with the actual day distance", () => {
    render(form("2026-10-12"));
    expect(screen.getByText(/^Galioja iki .+, po 2 dienų nuo šiandien\.$/)).toBeInTheDocument();
    expect(screen.getByLabelText("Expires on")).toHaveValue("2026-10-12");
    expect(screen.queryByText(/^Expires on .+ days from today\.$/)).not.toBeInTheDocument();
  });
});
