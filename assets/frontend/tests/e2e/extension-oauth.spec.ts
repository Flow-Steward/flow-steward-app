import { readFileSync } from "node:fs";

import { expect, test, type Page } from "@playwright/test";

type AcceptanceState = {
  extension_id: string;
  install_id: string;
  alpha_project_id: string;
  beta_project_id: string;
  alpha_configuration_ids: string[];
};

const statePath = process.env.FS_OAUTH_ACCEPTANCE_STATE;
if (!statePath) {
  throw new Error("FS_OAUTH_ACCEPTANCE_STATE is required");
}
const acceptance = JSON.parse(readFileSync(statePath, "utf8")) as AcceptanceState;
const operator = {
  email: process.env.E2E_OPERATOR_USERNAME || "",
  password: process.env.E2E_OPERATOR_PASSWORD || "",
};
const viewer = {
  email: process.env.E2E_VIEWER_USERNAME || "",
  password: process.env.E2E_VIEWER_PASSWORD || "",
};

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function firstText(...values: unknown[]): string {
  return values.map(text).find(Boolean) ?? "";
}

function oauthPrefix(projectId: string) {
  return `/api/projects/${projectId}/extensions/${acceptance.install_id}/oauth/example`;
}

async function login(page: Page, credentials: { email: string; password: string }) {
  await page.context().clearCookies();
  const response = await page.request.post("/api/auth/login", {
    data: { ...credentials, remember_me: false },
  });
  expect(response.status()).toBe(200);
  const payload = await response.json() as { csrf_token: string };
  return { "x-csrf-token": payload.csrf_token };
}

async function configurations(page: Page, projectId: string) {
  const response = await page.request.get(`${oauthPrefix(projectId)}/configurations`);
  expect(response.status()).toBe(200);
  return await response.json() as Array<Record<string, unknown>>;
}

async function chooseConfiguration(page: Page, controlName: string, displayName: string) {
  const selector = page.getByRole("combobox", { name: controlName, exact: true });
  await selector.click();
  await page.getByRole("listbox", { name: `${controlName} options`, exact: true })
    .getByRole("option", { name: displayName, exact: true }).click();
  await expect(selector).toHaveText(displayName);
  await expect(selector).toHaveAttribute("aria-expanded", "false");
}

async function startAuthorization(
  page: Page,
  headers: Record<string, string>,
  options: {
    configurationId: string;
    emailHint: string;
    returnPageId: "mail";
    existingConnectionId?: string;
  },
) {
  const response = await page.request.post(
    `${oauthPrefix(acceptance.alpha_project_id)}/authorization`,
    {
      headers,
      data: {
        configuration_id: options.configurationId,
        connection_type: "example_mail",
        intent_id: "connect",
        display_name: `Fixture ${options.emailHint}`,
        return_page_id: options.returnPageId,
        intent_inputs: { email_hint: options.emailHint },
        ...(options.existingConnectionId
          ? { existing_connection_id: options.existingConnectionId }
          : {}),
      },
    },
  );
  expect(response.status()).toBe(200);
  return await response.json() as { authorization_url: string };
}

test.describe.configure({ mode: "serial" });

test("real operator UI explains required and bounded values on Save", async ({ page }) => {
  await login(page, operator);
  await page.goto(
    `/app/extensions/${acceptance.extension_id}`
    + `?project_id=${acceptance.alpha_project_id}&extension_page=oauth_setup`,
  );

  await expect(page.getByRole("heading", { name: "Example Identity" })).toBeVisible();
  const selector = page.getByRole("combobox", { name: "OAuth configuration" });
  await selector.click();
  const configurationsMenu = page.getByRole("listbox", { name: "OAuth configuration options" });
  await expect(configurationsMenu.getByRole("option", { name: "Primary application", exact: true })).toBeVisible();
  await expect(configurationsMenu.getByRole("option", { name: "Backup application", exact: true })).toBeVisible();
  await configurationsMenu.getByRole("option", { name: "Add configuration…", exact: true }).click();
  await expect(selector).toHaveText("Add configuration…");
  const blankName = page.getByLabel("Configuration name");
  await blankName.fill("New application");
  const createSave = page.getByRole("button", { name: "Save configuration" });
  await expect(createSave).toBeEnabled();
  const beforeInvalidCreate = await page.request.get(
    `${oauthPrefix(acceptance.alpha_project_id)}/configurations`,
  );
  expect(beforeInvalidCreate.status()).toBe(200);
  const beforeInvalidCreateRows = await beforeInvalidCreate.json() as unknown[];
  await createSave.click();
  const missingClientId = page.getByLabel("Client ID");
  const missingClientSecret = page.getByLabel("Client secret");
  await expect(page.getByText("Client ID is required.")).toBeVisible();
  await expect(page.getByText("Client secret is required.")).toBeVisible();
  for (const field of [missingClientId, missingClientSecret]) {
    await expect(field).toHaveAttribute("aria-invalid", "true");
    await expect(field).toHaveAttribute("aria-describedby", /error/u);
  }
  await expect(missingClientId).toBeFocused();
  const afterInvalidCreate = await page.request.get(
    `${oauthPrefix(acceptance.alpha_project_id)}/configurations`,
  );
  expect(afterInvalidCreate.status()).toBe(200);
  expect(await afterInvalidCreate.json()).toHaveLength(beforeInvalidCreateRows.length);
  await chooseConfiguration(page, "OAuth configuration", "Primary application");
  await expect(page.getByText("Stored value will be kept.")).toBeVisible();
  const save = page.getByRole("button", { name: "Save configuration" });
  const clientId = page.getByLabel("Client ID");
  const clientSecret = page.getByLabel("Client secret");
  const browserOrigin = page.getByLabel("Browser origin");
  await expect(clientId).toHaveAttribute("type", "text");
  await expect(clientId).toHaveAttribute("aria-required", "true");
  await expect(clientSecret).toHaveAttribute("type", "password");
  await expect(clientSecret).toHaveAttribute("aria-required", "true");
  await expect(page.getByText(/^Client ID\s*\*$/u)).toBeVisible();
  await expect(page.getByText(/^Client secret\s*\*$/u)).toBeVisible();
  await expect(browserOrigin).toHaveAttribute("type", "text");
  await expect(browserOrigin).not.toHaveAttribute("aria-required", "true");
  await expect(page.getByText(/^Browser origin\s*\*$/u)).toHaveCount(0);
  await expect(save).toBeDisabled();
  const beforeInvalidOriginRows = await configurations(page, acceptance.alpha_project_id);
  const beforeInvalidOrigin = beforeInvalidOriginRows.find(
    (row) => row.configuration_id === acceptance.alpha_configuration_ids[0],
  );
  await browserOrigin.fill("http://browser.example.test/path");
  await expect(save).toBeEnabled();
  await save.click();
  await expect(page.getByText("Browser origin must be a canonical HTTPS origin.")).toBeVisible();
  await expect(browserOrigin).toHaveAttribute("aria-invalid", "true");
  await expect(browserOrigin).toHaveAttribute("aria-describedby", /error/u);
  await expect(browserOrigin).toBeFocused();
  const afterInvalidOriginRows = await configurations(page, acceptance.alpha_project_id);
  expect(afterInvalidOriginRows.find(
    (row) => row.configuration_id === acceptance.alpha_configuration_ids[0],
  )?.revision).toBe(beforeInvalidOrigin?.revision);
  await browserOrigin.fill("  https://browser.example.test  ");
  await page.getByLabel("Configuration name").fill("Primary application renamed");
  await expect(save).toBeEnabled();
  await save.click();
  const impactDialog = page.getByRole("alertdialog", {
    name: "Confirm configuration impact",
  });
  await expect(impactDialog).toBeVisible();
  await expect(impactDialog.locator("li")).toHaveCount(0);
  await impactDialog.getByRole("button", { name: "Confirm and save" }).click();
  await expect(page.getByRole("status").filter({ hasText: /^Configuration saved\./u })).toBeVisible();
  await expect(page.getByText("Stored value will be kept.")).toBeVisible();
  await expect(clientSecret).toBeDisabled();
  await expect(page.getByLabel("Browser origin")).toHaveValue(
    "https://browser.example.test",
  );
  const afterValidOriginRows = await configurations(page, acceptance.alpha_project_id);
  const persistedOrigin = (afterValidOriginRows.find(
    (row) => row.configuration_id === acceptance.alpha_configuration_ids[0],
  )?.fields as Array<Record<string, unknown>>).find(
    (field) => field.field_id === "browser_origin",
  );
  expect(persistedOrigin?.value).toBe("https://browser.example.test");

  await page.getByRole("button", { name: "Replace" }).click();
  await expect(page.getByRole("status").filter({ hasText: /^Configuration saved\./u })).toHaveCount(0);
  const replacement = page.getByLabel("Client secret");
  await replacement.fill("abc");
  await expect(save).toBeEnabled();
  await save.click();
  const minimumError = page.getByText("Client secret must be at least 4 characters.");
  await expect(minimumError).toBeVisible();
  await expect(replacement).toHaveAttribute("aria-invalid", "true");
  await expect(replacement).toHaveAttribute("aria-describedby", /error/u);
  await expect(replacement).toBeFocused();
  await replacement.fill("fixed-credential");
  await expect(save).toBeEnabled();
  await save.click();
  await expect(page.getByRole("status").filter({ hasText: /^Configuration saved\./u })).toContainText("revalidated on the next use");
});

test("service rejects a browser-bypass mutation and viewer reads remain redacted", async ({ page }) => {
  const operatorHeaders = await login(page, operator);
  const invalid = await page.request.put(
    `${oauthPrefix(acceptance.alpha_project_id)}/configurations`,
    {
      headers: operatorHeaders,
      data: {
        display_name: "Bypass",
        values: { client_id: "" },
        secret_replacements: {},
      },
    },
  );
  expect(invalid.status()).toBe(400);
  expect(await invalid.json()).toMatchObject({
    error: "invalid_credential_field",
    field_id: "client_id",
  });

  const tooLong = await page.request.put(
    `${oauthPrefix(acceptance.alpha_project_id)}/configurations`,
    {
      headers: operatorHeaders,
      data: {
        display_name: "Bypass maximum",
        values: { client_id: "x".repeat(41) },
        secret_replacements: { client_secret: "valid-secret" },
      },
    },
  );
  expect(tooLong.status()).toBe(400);
  expect(await tooLong.json()).toMatchObject({
    error: "invalid_credential_field",
    field_id: "client_id",
  });
  for (const secret of ["abc", "x".repeat(81)]) {
    const invalidSecret = await page.request.put(
      `${oauthPrefix(acceptance.alpha_project_id)}/configurations`,
      {
        headers: operatorHeaders,
        data: {
          display_name: "Bypass secret length",
          values: { client_id: "valid-client" },
          secret_replacements: { client_secret: secret },
        },
      },
    );
    expect(invalidSecret.status()).toBe(400);
    expect(await invalidSecret.json()).toMatchObject({
      error: "invalid_credential_field",
      field_id: "client_secret",
    });
  }

  await login(page, viewer);
  const alpha = await configurations(page, acceptance.alpha_project_id);
  const beta = await configurations(page, acceptance.beta_project_id);
  expect(alpha.map((row) => row.display_name)).toEqual([
    "Backup application",
    "Primary application renamed",
  ]);
  expect(beta.map((row) => row.display_name)).toEqual(["Isolated application"]);
  expect(alpha.every((row) => Array.isArray(row.fields) && row.fields.length === 0)).toBe(true);
  expect(JSON.stringify(alpha)).not.toContain("secret_present");
});

test("real provider success, denial, reconnect failure, and closed redirect stay scoped", async ({ page }) => {
  const headers = await login(page, operator);
  const primary = acceptance.alpha_configuration_ids[0];
  await page.goto(
    `/app/extensions/${acceptance.extension_id}`
    + `?project_id=${acceptance.alpha_project_id}&extension_page=files`,
  );
  const folderHint = page.getByLabel("Folder hint");
  await expect(folderHint).toHaveAttribute("type", "text");
  await expect(folderHint).toHaveAttribute("maxlength", "120");
  await folderHint.fill("Quarterly records");
  await expect(folderHint).toHaveValue("Quarterly records");
  await folderHint.clear();

  await page.goto(
    `/app/extensions/${acceptance.extension_id}`
    + `?project_id=${acceptance.alpha_project_id}&extension_page=mail`,
  );
  const connectionName = page.getByLabel(/Connection name/u);
  const emailHint = page.getByLabel("Email hint");
  await chooseConfiguration(page, "Configuration for new connection", "Primary application renamed");
  await expect(emailHint).toHaveAttribute("type", "email");
  await connectionName.fill("Cancelled authorization");
  await emailHint.fill("not-an-email");
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByText("Email hint must be a valid email address.")).toBeVisible();
  await expect(emailHint).toHaveAttribute("aria-invalid", "true");
  await expect(emailHint).toHaveAttribute("aria-describedby", /error/u);
  await expect(emailHint).toBeFocused();
  await emailHint.fill("deny@example.test");
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page).toHaveURL(/oauth=error&reason=authorization_cancelled/u);
  await expect(page.getByText(/Authorization was cancelled/u)).toBeVisible();

  await page.goto(
    `/app/extensions/${acceptance.extension_id}`
    + `?project_id=${acceptance.alpha_project_id}&extension_page=mail`,
  );
  await expect(connectionName).toBeVisible();
  await expect(emailHint).toBeVisible();
  await chooseConfiguration(page, "Configuration for new connection", "Primary application renamed");
  await connectionName.fill("Retry after cancellation");
  await emailHint.fill("success@example.test");
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  const escapedExtensionId = acceptance.extension_id.replace(".", String.raw`\.`);
  await expect(page).toHaveURL(new RegExp(
    `/extensions/${escapedExtensionId}`
    + String.raw`\?project_id=${acceptance.alpha_project_id}&extension_page=mail$`,
    "u",
  ));
  await expect(page.getByText("Neutral Verified Account connected successfully.")).toBeVisible();
  await expect(page.getByText("External account connected successfully.")).toHaveCount(0);
  expect(page.url()).not.toContain("oauth_connection_id=");
  expect(page.url()).not.toContain("state=");
  expect(page.url()).not.toContain("code=");
  expect(page.url()).not.toContain("token");

  const connectionsResponse = await page.request.get(
    `/api/projects/${acceptance.alpha_project_id}/connections`,
  );
  expect(connectionsResponse.status()).toBe(200);
  const connectionsPayload = await connectionsResponse.json() as { connections?: Array<Record<string, unknown>> } | Array<Record<string, unknown>>;
  const connections = Array.isArray(connectionsPayload)
    ? connectionsPayload
    : connectionsPayload.connections || [];
  const connected = connections.find((row) => row.oauth_configuration_id === primary);
  expect(connected).toBeTruthy();

  await page.goto(
    `/app/extensions/${acceptance.extension_id}`
    + `?project_id=${acceptance.alpha_project_id}&extension_page=oauth_setup`,
  );
  await chooseConfiguration(page, "OAuth configuration", "Primary application renamed");
  await page.getByRole("button", { name: "Replace" }).click();
  await page.getByLabel("Client secret").fill("fixture-credential");
  await page.getByRole("button", { name: "Save configuration" }).click();
  await expect(page.getByRole("status").filter({ hasText: /^Configuration saved\./u })).toContainText("revalidated on the next use");
  const revalidated = await configurations(page, acceptance.alpha_project_id);
  expect(revalidated.find((row) => row.configuration_id === primary)?.status).toBe(
    "ready_for_authorization",
  );
  const retainedAfterRevalidation = await page.request.get(
    `/api/projects/${acceptance.alpha_project_id}/connections`,
  );
  expect(JSON.stringify(await retainedAfterRevalidation.json())).toContain(
    String(connected?.connection_id),
  );
  await page.goto(
    `/app/extensions/${acceptance.extension_id}`
    + `?project_id=${acceptance.alpha_project_id}&extension_page=mail`,
  );
  const runtimeTestResponsePromise = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && response.url().includes(
      `/api/extensions/${acceptance.extension_id}/action/test_runtime_connection`,
    )
  ));
  await page.getByRole("button", {
    name: `Test ${firstText(connected?.display_name, connected?.connection_id)}`,
  }).first().click();
  const runtimeTestResponse = await runtimeTestResponsePromise;
  const runtimeTestPayload = await runtimeTestResponse.json() as Record<string, unknown>;
  expect(runtimeTestResponse.status(), JSON.stringify(runtimeTestPayload)).toBe(200);
  await expect(page.getByText("Connection test successful.")).toBeVisible();
  const verifiedAfterRuntimeUse = await configurations(page, acceptance.alpha_project_id);
  expect(verifiedAfterRuntimeUse.find((row) => row.configuration_id === primary)?.status).toBe(
    "verified",
  );
  const runtimeConnectionsResponse = await page.request.get(
    `/api/projects/${acceptance.alpha_project_id}/connections`,
  );
  const runtimeConnectionsPayload = await runtimeConnectionsResponse.json() as { connections?: Array<Record<string, unknown>> } | Array<Record<string, unknown>>;
  const runtimeConnections = Array.isArray(runtimeConnectionsPayload)
    ? runtimeConnectionsPayload
    : runtimeConnectionsPayload.connections || [];
  const runtimeConnection = runtimeConnections.find(
    (row) => row.connection_id === connected?.connection_id,
  );
  expect(runtimeConnection?.connection_id).toBe(connected?.connection_id);
  expect(Number(runtimeConnection?.credential_revision)).toBeGreaterThan(
    Number(connected?.credential_revision),
  );

  const postConnectFailure = await startAuthorization(page, headers, {
    configurationId: primary,
    emailHint: "post_connect_failure@example.test",
    returnPageId: "mail",
  });
  await page.goto(postConnectFailure.authorization_url);
  await expect(page).toHaveURL(/oauth=error&reason=provider_configuration_error/u);
  const pendingResponse = await page.request.get(
    `/api/projects/${acceptance.alpha_project_id}/connections`,
  );
  const pendingPayload = await pendingResponse.json() as { connections?: Array<Record<string, unknown>> } | Array<Record<string, unknown>>;
  const pendingRows = Array.isArray(pendingPayload) ? pendingPayload : pendingPayload.connections || [];
  const pending = pendingRows.find((row) => row.display_name === "Fixture post_connect_failure@example.test");
  expect(pending?.connection_config).toMatchObject({
    oauth: { lifecycle_status: "needs_attention" },
  });
  await page.goto(
    `/app/extensions/${acceptance.extension_id}`
    + `?project_id=${acceptance.alpha_project_id}&extension_page=mail`,
  );
  const pendingConnectionId = text(pending?.connection_id);
  expect(pendingConnectionId).not.toBe("");
  const retryResponsePromise = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && response.url().includes(
      `${oauthPrefix(acceptance.alpha_project_id)}/connections/`
      + `${encodeURIComponent(pendingConnectionId)}/post-connect/retry`,
    )
  ));
  const pendingRow = page.getByLabel(
    `Connection ${firstText(pending?.display_name, pendingConnectionId)}`,
  );
  await pendingRow.getByRole("button", { name: "Retry" }).click();
  const retryResponse = await retryResponsePromise;
  const retryPayload = await retryResponse.json() as Record<string, unknown>;
  expect(retryResponse.status(), JSON.stringify(retryPayload)).toBe(200);
  expect(retryPayload).toMatchObject({
    outcome: "connected",
    connection_id: pendingConnectionId,
  });
  const retriedRow = page.locator(
    `[id="extension-oauth-connection-${encodeURIComponent(pendingConnectionId)}"]`,
  );
  await expect(retriedRow.getByText("Neutral Verified Account")).toBeVisible();
  const retriedResponse = await page.request.get(
    `/api/projects/${acceptance.alpha_project_id}/connections`,
  );
  const retriedPayload = await retriedResponse.json() as { connections?: Array<Record<string, unknown>> } | Array<Record<string, unknown>>;
  const retriedRows = Array.isArray(retriedPayload) ? retriedPayload : retriedPayload.connections || [];
  expect(retriedRows.find((row) => row.connection_id === pendingConnectionId)?.connection_config).toMatchObject({
    oauth: { lifecycle_status: "connected" },
  });

  const connectedConnectionId = text(connected?.connection_id);
  expect(connectedConnectionId).not.toBe("");
  const reconnect = await startAuthorization(page, headers, {
    configurationId: primary,
    emailHint: "invalid_grant@example.test",
    returnPageId: "mail",
    existingConnectionId: connectedConnectionId,
  });
  await page.goto(reconnect.authorization_url);
  await expect(page).toHaveURL(/oauth=error&reason=authorization_required/u);
  await expect(page).toHaveURL(/oauth_connection_preserved=1/u);
  await expect(page.getByText(
    `${String(connected?.display_name)} reauthorization did not complete. The existing connection and its previous authorization were not replaced and remain in their prior state.`,
  )).toBeVisible();
  const preservedRow = page.locator(
    `[id="extension-oauth-connection-${encodeURIComponent(connectedConnectionId)}"]`,
  );
  await expect(preservedRow).toBeFocused();
  await expect(preservedRow).toHaveClass(/\bis-focused\b/u);
  await expect(page.getByRole("button", { name: /Reconnect/u })).toHaveCount(0);
  await expect.poll(() => {
    const callbackUrl = new URL(page.url());
    return ["oauth", "reason", "oauth_connection_id", "oauth_connection_preserved"]
      .some((name) => callbackUrl.searchParams.has(name));
  }).toBe(false);
  const after = await page.request.get(
    `/api/projects/${acceptance.alpha_project_id}/connections`,
  );
  const afterPayload = await after.json() as { connections?: Array<Record<string, unknown>> } | Array<Record<string, unknown>>;
  const afterRows = Array.isArray(afterPayload) ? afterPayload : afterPayload.connections || [];
  const exact = afterRows.find((row) => row.connection_id === connectedConnectionId);
  expect(exact?.connection_config).toMatchObject({
    oauth: { lifecycle_status: "connected" },
  });
  await page.goto(
    `/app/extensions/${acceptance.extension_id}`
    + `?project_id=${acceptance.alpha_project_id}&extension_page=mail`,
  );
  await expect(page.getByText("Authorization is required again.")).not.toBeVisible();
  await expect(page.getByRole("button", { name: /Reconnect/u })).toHaveCount(0);

  const unavailable = await startAuthorization(page, headers, {
    configurationId: primary,
    emailHint: "unavailable@example.test",
    returnPageId: "mail",
  });
  await page.goto(unavailable.authorization_url);
  await expect(page).toHaveURL(/oauth=error&reason=provider_unavailable/u);
  await expect(page.getByText(/temporarily unavailable/u)).toBeVisible();
  expect(await page.textContent("body")).not.toContain("temporarily_unavailable");
});

test("invalid client targets only the selected configuration with safe guidance", async ({ page }) => {
  const headers = await login(page, operator);
  const selected = acceptance.alpha_configuration_ids[1];
  const flow = await startAuthorization(page, headers, {
    configurationId: selected,
    emailHint: "invalid_client@example.test",
    returnPageId: "mail",
  });
  await page.goto(flow.authorization_url);

  await expect(page).toHaveURL(/oauth=error&reason=provider_configuration_error/u);
  await expect(page.getByText(/OAuth configuration needs attention/u)).toBeVisible();
  const rows = await configurations(page, acceptance.alpha_project_id);
  expect(rows.find((row) => row.configuration_id === selected)?.status).toBe("needs_attention");
  expect(rows.find((row) => row.configuration_id !== selected)?.status).toBe("verified");
  expect(await page.textContent("body")).not.toContain("invalid_client");
});

test("viewer navigates isolated projects without OAuth mutation controls", async ({ page }) => {
  await login(page, viewer);
  await page.goto(
    `/app/extensions/${acceptance.extension_id}`
    + `?project_id=${acceptance.alpha_project_id}&extension_page=mail`,
  );
  await expect(page.getByText("Neutral Verified Account").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Connect" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Retry" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Reconnect/u })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Disconnect" })).toHaveCount(0);

  await page.goto(
    `/app/extensions/${acceptance.extension_id}`
    + `?project_id=${acceptance.beta_project_id}&extension_page=oauth_setup`,
  );
  await expect(page.getByText("Isolated application")).toBeVisible();
  await expect(page.getByText("Primary application renamed")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Save configuration" })).toHaveCount(0);
});
