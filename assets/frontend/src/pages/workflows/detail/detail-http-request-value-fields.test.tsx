import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { HttpMapField, HttpValueField } from "./detail-http-request-value-fields";
import { useHttpRequestIntent } from "./detail-http-request-intent";
function Harness({ map = false }: { map?: boolean }) {
  const [value, setValue] = useState<unknown>(map ? { "2": "001", Flag: "false", Json: '{"a":1}' } : { n: 0, flag: false });
  const intent = useHttpRequestIntent("A", () => undefined);
  const props = { intent, value, onChange: setValue, renderVariableInput: ({ ariaLabel, value: text, onChange }: { ariaLabel: string; value: string; onChange: (text: string) => void }) => <input aria-label={ariaLabel} value={text} onChange={(event) => onChange(event.target.value)} /> };
  return <>{map ? <HttpMapField {...props} field="headers" label="Headers" /> : <HttpValueField {...props} field="body_template" label="Body" />}<output data-testid="typed">{JSON.stringify(value)}</output></>;
}
async function choose(label: string, option: string) {
  await userEvent.click(screen.getByRole("combobox", { name: label }));
  await userEvent.click(within(screen.getByRole("listbox", { name: `${label} options` })).getByRole("option", { name: option }));
}
it("uses an explicit Text conversion and keeps text false as a string", async () => {
  render(<Harness />);
  await choose("Body value format", "Text");
  expect(JSON.parse(screen.getByTestId("typed").textContent || "null")).toBe('{"n":0,"flag":false}');
  fireEvent.change(screen.getByLabelText("Body"), { target: { value: "false" } });
  expect(JSON.parse(screen.getByTestId("typed").textContent || "null")).toBe("false");
  await choose("Body value format", "JSON");
  expect(screen.getByLabelText("Body")).toHaveValue('"false"');
  fireEvent.change(screen.getByLabelText("Body"), { target: { value: "false" } });
  expect(JSON.parse(screen.getByTestId("typed").textContent || "null")).toBe(false);
});
it("renames a numeric key without moving sibling raw/value identity", () => {
  render(<Harness map />);
  const second = screen.getByLabelText("Headers value 2");
  fireEvent.change(screen.getByLabelText("Headers key 1"), { target: { value: "20" } });
  expect(screen.getByLabelText("Headers value 2")).toBe(second);
  expect(second).toHaveValue("false");
  expect(JSON.parse(screen.getByTestId("typed").textContent || "null")).toEqual({ "20": "001", Flag: "false", Json: '{"a":1}' });
});
