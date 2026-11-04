import { inputNameFromAuthPart, storedSecretInputName } from "./detail-http-auth-model";

describe("HTTP auth input names", () => {
  it.each([
    ["{{\tstate.inputs._token2 \n}}", "_token2"],
    ["{{ state.inputs.2token }}", ""],
    ["{{ state.inputs.token-name }}", ""],
  ])("preserves the accepted template name syntax: %s", (template, name) => {
    expect(inputNameFromAuthPart(template)).toBe(name);
  });

  it.each([
    ["__ Shop__API key __", "shop_api_key"],
    ["__ Éclair API __", "e_clair_api"],
    ["___", "secret"],
  ])("trims boundary separators after normalizing a secret name: %s", (name, input) => {
    expect(storedSecretInputName(name, [])).toBe(input);
  });
});
