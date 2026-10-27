import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { JsonCollectionWorkbench } from "./json-collection-workbench";

it("localizes deletion confirmation while preserving the selected record ID and refusing a declined delete", async () => {
  const confirm = vi.spyOn(globalThis, "confirm").mockReturnValue(false);
  const deleteItem = vi.fn();
  const messages: Record<string, string> = { "react.shared.confirm_delete_record": "Ištrinti {id}?" };
  render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><JsonCollectionWorkbench eyebrow="Fixture" title="Records" description="Fixture collection" listQueryKey={["fixture", "records"]} fetchList={async () => [{ id: "owner-record-42", display_name: "Owner record" }]} idKey="id" deleteItem={deleteItem} /></MemoryRouter></QueryClientProvider></I18nContext.Provider>);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /Owner record/ }));
  await user.click(screen.getByRole("button", { name: "Delete selected" }));
  expect(confirm).toHaveBeenCalledWith("Ištrinti owner-record-42?");
  expect(deleteItem).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: /Owner record/ })).toBeInTheDocument();
  confirm.mockRestore();
});
