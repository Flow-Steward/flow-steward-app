import { fileURLToPath } from "node:url";

import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";

const bootstrap = {
  status: "ok",
  auth: {
    authenticated: true,
    local_auth_enabled: true,
    bootstrap_required: false,
    external_auth_enabled: false,
    auth_source: "local",
    user: {
      id: "u1",
      email: "maya@acme.dev",
      username: "maya@acme.dev",
      display_name: "Maya Chen",
      roles: ["admin"],
      account_id: "acc-1",
      account_ids: ["acc-1", "acc-2"],
      account_memberships: [
        { account_id: "acc-1", account_name: "Main account", role: "admin", is_active: true, is_default: true },
        { account_id: "acc-2", account_name: "Analytics", role: "operator", is_active: true },
      ],
      timezone: "Europe/Berlin",
      locale: "en",
    },
    roles: ["admin"],
    permissions: { can_read: true, can_mutate: true },
    account_memberships: [],
    external_sign_in: {},
  },
  i18n: { locale: "en", fallback_locale: "en", supported_locales: [{ code: "en", label: "English", native_label: "English" }, { code: "lt", label: "Lithuanian", native_label: "Lietuvių" }], messages: {} },
  feature_flags: {},
  environment: { edition: "community" },
  settings_pages: [],
  registered_pages: [],
  shell_navigation: {},
};

const users = [
  { id: "u1", email: "maya@acme.dev", username: "maya@acme.dev", display_name: "Maya Chen", roles: ["admin", "operator"], account_id: "acc-1", account_ids: ["acc-1", "acc-2"], is_active: true, must_change_password: false },
  { id: "u2", email: "dev@acme.dev", username: "dev@acme.dev", display_name: "Dev Patel", roles: ["operator"], account_id: "acc-1", account_ids: ["acc-1"], is_active: true, must_change_password: true },
  { id: "u3", email: "lena@acme.dev", username: "lena@acme.dev", display_name: "Lena Koh", roles: ["viewer"], account_id: "acc-2", account_ids: ["acc-2", "acc-3", "acc-1"], is_active: true, must_change_password: false },
  { id: "u4", email: "omar@acme.dev", username: "omar@acme.dev", display_name: "Omar Diallo", roles: ["viewer"], account_id: "acc-3", account_ids: ["acc-3"], is_active: false, must_change_password: true },
  { id: "u5", email: "rui@acme.dev", username: "rui@acme.dev", display_name: "Rui Alves", roles: ["admin"], account_id: "acc-1", account_ids: ["acc-1", "acc-2"], is_active: true, must_change_password: false },
];

async function json(route: Route, body: unknown) {
  await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
}

async function installFixtures(page: Page) {
  const inventory = [...users];
  // The backend serves these real assets in production; Vite preview serves only dist.
  await page.route("**/static/platform-kit/**", async (route) => {
    const relativePath = new URL(route.request().url()).pathname.slice("/static/platform-kit/".length);
    const asset = new URL(relativePath, new URL("../../../platform-kit/", import.meta.url));
    await route.fulfill({ path: fileURLToPath(asset) });
  });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/app/bootstrap") return json(route, bootstrap);
    if (path === "/api/auth/me") return json(route, bootstrap.auth);
    if (path === "/api/auth/accounts/current") return json(route, { account: { account_id: "acc-1", display_name: "Flow Steward" } });
    if (path === "/api/auth/accounts") return json(route, { accounts: [{ account_id: "acc-1", display_name: "Flow Steward" }, { account_id: "acc-2", display_name: "Analytics" }, { account_id: "acc-3", display_name: "Operations" }] });
    if (path === "/api/auth/users" && route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      const created = { ...body, id: "u6", username: body.email, account_id: "acc-1", account_ids: ["acc-1"] };
      inventory.push(created);
      return json(route, { status: "created", user: created });
    }
    if (path === "/api/auth/users/u2" && route.request().method() === "PUT") {
      const body = route.request().postDataJSON();
      inventory[1] = { ...inventory[1], ...body };
      return json(route, { user: inventory[1] });
    }
    if (path === "/api/auth/users") return json(route, { users: inventory, total: inventory.length, page: 1, per_page: 5, total_pages: 1 });
    if (path === "/api/users/me/email-change-status") return json(route, {});
    if (path === "/api/auth/tokens" && route.request().method() === "POST") {
      return json(route, {
        token: {
          id: "token-created",
          label: "Local CLI",
          raw_token: "fs_pat_test_secret",
        },
      });
    }
    if (path === "/api/auth/tokens") return json(route, { tokens: [], total: 0, page: 1, per_page: 5, total_pages: 0 });
    if (path === "/api/v2/projects") return json(route, { projects: [{ id: "7", name: "Platform" }, { id: "8", name: "Customer portal" }, { id: "9", name: "Internal tooling" }], total: 3, page: 1, per_page: 200, total_pages: 1 });
    if (path === "/api/workflows") return json(route, [{ workflow_id: "wf-1", display_name: "Contextual Bugfix" }, { workflow_id: "wf-2", display_name: "Incident Response" }, { workflow_id: "wf-3", display_name: "Maintenance" }]);
    return json(route, { status: "ok" });
  });
}

async function waitForAnimations(element: ReturnType<Page["getByRole"]>) {
  await element.evaluate(async (node) => {
    await Promise.all(node.getAnimations({ subtree: true }).map((animation) => animation.finished));
  });
}

async function expectNoSeriousAccessibilityIssues(page: Page, selector: string) {
  const result = await new AxeBuilder({ page }).include(selector).analyze();
  const blocking = result.violations
    .filter((violation) => violation.impact === "serious" || violation.impact === "critical")
    .map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      nodes: violation.nodes.map((node) => ({ target: node.target, summary: node.failureSummary })),
    }));
  expect(blocking).toEqual([]);
}

test.beforeEach(async ({ page }) => installFixtures(page));

test("OSS user creation and editing need no tenant selection", async ({ page }, testInfo) => {
  const errors: string[] = [];
  const tenantRequests: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/api/auth/accounts")) tenantRequests.push(request.url());
  });
  await page.goto("/app/settings/users");
  await expect(page.getByRole("heading", { name: "Users & access" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Accounts" })).toHaveCount(0);
  await page.getByRole("button", { name: "Add user" }).click();
  const dialog = page.getByRole("dialog", { name: "Add user" });
  await expect(dialog.getByRole("combobox", { name: "Account memberships" })).toHaveCount(0);
  await expect(dialog.getByRole("combobox", { name: "Primary account" })).toHaveCount(0);
  await dialog.getByLabel("Display name").fill("New User");
  await dialog.getByLabel("Email address").fill("new@example.com");
  await dialog.getByLabel("Temporary password", { exact: true }).fill("temporary-pass");
  const createRequest = page.waitForRequest((request) => request.url().endsWith("/api/auth/users") && request.method() === "POST");
  await dialog.getByRole("button", { name: "Add user", exact: true }).click();
  expect((await createRequest).postDataJSON()).toEqual({
    display_name: "New User", email: "new@example.com", password: "temporary-pass", // pragma: allowlist secret - disposable user fixture
    roles: ["viewer"], is_active: true, must_change_password: true,
  });
  await expect(dialog).toBeHidden();
  await expect(page.getByText("New User was added.", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Actions for Dev Patel" }).click();
  await page.getByRole("menuitem", { name: "Edit access" }).click();
  const edit = page.getByRole("dialog", { name: "Edit access for Dev Patel" });
  await expect(edit.getByRole("combobox", { name: "Account memberships" })).toHaveCount(0);
  await edit.getByLabel("Display name").fill("Dev Updated");
  const updateRequest = page.waitForRequest((request) => request.url().endsWith("/api/auth/users/u2") && request.method() === "PUT");
  await edit.getByRole("button", { name: "Save changes" }).click();
  expect((await updateRequest).postDataJSON()).toEqual({ display_name: "Dev Updated" });
  await expect(edit).toBeHidden();
  await expect(page.getByRole("button", { name: "Actions for Dev Updated" })).toBeVisible();
  await expect(page.getByRole("table", { name: "Users and access" })).toBeVisible();
  expect(tenantRequests).toEqual([]);
  expect(errors).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("oss-users-inventory.png"), fullPage: false });
});

test("OSS profile and user menu hide tenants but retain personal controls", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/app/settings/account");
  await expect(page.getByRole("heading", { name: "Profile", exact: true })).toBeVisible();
  await expect(page.getByText(/Active account/)).toHaveCount(0);
  await expect(page.getByText("Main account", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "Sign-in" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Personal tokens" })).toBeVisible();
  await page.getByRole("button", { name: "Account menu" }).click();
  const menu = page.getByRole("menu", { name: "Account menu" });
  await expect(menu.getByRole("group", { name: "Accounts" })).toHaveCount(0);
  await expect(menu.getByRole("menuitem", { name: "Account profile" })).toBeVisible();
  await expect(menu.getByRole("menuitem", { name: "Log out" })).toBeVisible();
  await expect(menu.getByRole("menuitemradio", { name: "Dark theme" })).toBeVisible();
  expect(errors).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("oss-profile-menu.png"), fullPage: false });
});

test("users inventory, row menu and add-user dialog match the released pattern", async ({ page }) => {
  await page.goto("/app/settings/users");
  await expect(page.getByRole("heading", { name: "Users & access" })).toBeVisible();
  await expect(page.getByRole("table", { name: "Users and access" })).toBeVisible();
  await page.getByRole("button", { name: "Actions for Rui Alves" }).focus();
  await page.keyboard.press("ArrowDown");
  const menu = page.getByRole("menu", { name: "Actions for Rui Alves" });
  await expect(menu).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Edit access" })).toBeFocused();
  await page.keyboard.press("End");
  await expect(page.getByRole("menuitem", { name: "Deactivate user" })).toBeFocused();
  const menuBox = await menu.boundingBox();
  const footerBox = await page.locator(".fs-table-footer").boundingBox();
  expect(menuBox).not.toBeNull();
  expect((menuBox?.x || 0) + (menuBox?.width || 0)).toBeLessThanOrEqual(page.viewportSize()?.width || 1280);
  expect((menuBox?.y || 0) + (menuBox?.height || 0)).toBeLessThanOrEqual(footerBox?.y || page.viewportSize()?.height || 720);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Add user" }).click();
  const dialog = page.getByRole("dialog", { name: "Add user" });
  await expect(dialog).toBeVisible();
  await waitForAnimations(dialog);
  expect(await dialog.evaluate((element) => getComputedStyle(element).width)).toBe("600px");
  const checkbox = dialog.locator(".fs-checkbox-mark").first();
  const label = dialog.getByText("Viewer", { exact: true });
  const [checkboxBox, labelBox] = await Promise.all([checkbox.boundingBox(), label.boundingBox()]);
  expect(Math.abs(((checkboxBox?.y || 0) + (checkboxBox?.height || 0) / 2) - ((labelBox?.y || 0) + (labelBox?.height || 0) / 2))).toBeLessThanOrEqual(1);
  await page.screenshot({ path: "../../logs/test-runs/playwright/users-access-add-dialog.png", fullPage: false });
});

test("account profile and sign-in use the connected settings pattern", async ({ page }) => {
  await page.goto("/app/settings/account");
  await expect(page.getByRole("heading", { name: "Account" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Profile" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Profile" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("combobox", { name: "Timezone" })).toHaveValue("Europe/Berlin");
  await expect(page.getByRole("combobox", { name: "Interface language" })).toBeVisible();
  await page.getByRole("tab", { name: "Sign-in" }).click();
  await expect(page.getByRole("heading", { name: "Password" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Current password" })).toBeVisible();
  await page.screenshot({ path: "../../logs/test-runs/playwright/account-signin.png", fullPage: false });
});

test("token restriction Combobox uses the full field and modal popup layer", async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem("platform.ui_theme", "light");
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async () => undefined },
    });
  });
  await page.goto("/app/settings/account?tab=tokens");
  await expect(page.getByRole("heading", { name: "Personal access tokens" })).toBeVisible();
  await page.getByRole("button", { name: "Create token" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Create personal token" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("combobox", { name: "Workflow restrictions" }).click();
  await page.getByRole("option", { name: "Contextual Bugfix" }).click();
  await page.getByRole("option", { name: "Incident Response" }).click();
  await page.getByRole("option", { name: "Maintenance" }).click();

  const summary = dialog.locator(".fs-multi-summary").last();
  await expect(summary.getByRole("button", { name: /^Remove / })).toHaveCount(3);
  const fit = await summary.evaluate((element) => ({ clientWidth: element.clientWidth, scrollWidth: element.scrollWidth }));
  expect(fit.scrollWidth).toBeLessThanOrEqual(fit.clientWidth);
  const queryWidth = await dialog.getByRole("combobox", { name: "Workflow restrictions" }).evaluate((element) => element.getBoundingClientRect().width);
  expect(queryWidth).toBeLessThanOrEqual(2.5);

  const listbox = page.getByRole("listbox", { name: "Workflow restrictions options" });
  const [listboxBox, footerBox] = await Promise.all([listbox.boundingBox(), dialog.locator(".fs-dialog-footer").boundingBox()]);
  await expect(listbox).toHaveCSS("z-index", "520");
  expect((listboxBox?.y || 0) + (listboxBox?.height || 0)).toBeLessThanOrEqual(footerBox?.y || page.viewportSize()?.height || 720);
  await page.screenshot({ path: "../../logs/test-runs/playwright/account-token-restrictions-light-1280.png", fullPage: false });

  await summary.getByRole("button", { name: "Remove Incident Response" }).click();
  await expect(summary.getByRole("button", { name: /^Remove / })).toHaveCount(2);

  await dialog.getByLabel("Label").fill("Local CLI");
  await dialog.getByRole("button", { name: "Create token" }).click();
  const secretDialog = page.getByRole("dialog", { name: "Save your personal token" });
  await expect(secretDialog).toBeVisible();
  await waitForAnimations(secretDialog);
  await expect(secretDialog.getByRole("button", { name: "Close one-time token secret" })).toBeVisible();
  await expect(secretDialog.getByText("Store this value in a secure credential manager.")).toBeVisible();
  const secretInput = secretDialog.getByRole("textbox", { name: "One-time token secret" });
  await expect(secretInput).toHaveAttribute("type", "password");
  await expect(secretDialog.getByRole("button", { name: "Show one-time token secret" }).locator(".lucide-eye-off")).toHaveCount(1);
  const copyButton = secretDialog.getByRole("button", { name: "Copy token" });
  await expect(copyButton.locator("svg")).toHaveCount(0);
  await copyButton.click();
  await expect(page.getByRole("status").filter({ hasText: "Token copied to clipboard." })).toBeVisible();
  await page.screenshot({ path: "../../logs/test-runs/playwright/account-token-secret-light-1280.png", fullPage: false });
});

test("account and access surfaces have no serious accessibility violations", async ({ page }) => {
  await page.goto("/app/settings/users");
  await expect(page.getByRole("table", { name: "Users and access" })).toBeVisible();
  await expectNoSeriousAccessibilityIssues(page, ".fs-settings");
  await page.getByRole("button", { name: "Add user" }).click();
  const dialog = page.getByRole("dialog", { name: "Add user" });
  await expect(dialog).toBeVisible();
  await waitForAnimations(dialog);
  await expectNoSeriousAccessibilityIssues(page, ".fs-dialog");
  await page.keyboard.press("Escape");
  await page.goto("/app/settings/account");
  await expect(page.getByRole("heading", { name: "Profile" })).toBeVisible();
  await expectNoSeriousAccessibilityIssues(page, ".fs-settings");
  await page.getByRole("tab", { name: "Sign-in" }).click();
  await expect(page.getByRole("heading", { name: "Password" })).toBeVisible();
  await waitForAnimations(page.locator(".fs-tabs-list"));
  await expectNoSeriousAccessibilityIssues(page, ".fs-settings");
});

for (const theme of ["light", "dark"] as const) {
  for (const width of [1440, 1024, 768, 390]) {
    test(`${theme} settings patterns remain faithful at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
      await page.addInitScript((selectedTheme) => {
        window.localStorage.setItem("platform.ui_theme", selectedTheme);
      }, theme);

      await page.goto("/app/settings/users");
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await expect(page.getByRole("table", { name: "Users and access" })).toBeVisible();
      await expect(page.locator(".fs-management-table .fs-avatar").first()).toHaveCSS("width", "24px");
      await expect(page.locator(".fs-management-table tbody tr").first()).toHaveCSS("height", "49px");
      const managementMetrics = await page.evaluate(() => {
        const rect = (selector: string) => document.querySelector(selector)?.getBoundingClientRect();
        const searchStyles = getComputedStyle(document.querySelector(".fs-search") as Element);
        return {
          headerHeight: rect(".fs-management-table thead tr")?.height,
          rowHeight: rect(".fs-management-table tbody tr")?.height,
          avatar: rect(".fs-management-table .fs-avatar")?.width,
          searchHeight: rect(".fs-search")?.height,
          searchWidth: rect(".fs-search")?.width,
          searchGap: searchStyles.gap,
          toolbarGap: getComputedStyle(document.querySelector(".fs-table-toolbar") as Element).gap,
        };
      });
      expect(managementMetrics.headerHeight).toBeCloseTo(34.625, 2);
      expect(managementMetrics.rowHeight).toBe(49);
      expect(managementMetrics.avatar).toBe(24);
      expect(managementMetrics.searchHeight).toBe(32);
      expect(managementMetrics.searchGap).toBe("7px");
      expect(managementMetrics.toolbarGap).toBe("10px");
      if (width > 720) expect(managementMetrics.searchWidth).toBe(180);
      const tableViewport = await page.locator(".fs-table-scroll").boundingBox();
      expect(tableViewport).not.toBeNull();
      expect(tableViewport?.x || 0).toBeGreaterThanOrEqual(0);
      expect((tableViewport?.x || 0) + (tableViewport?.width || 0)).toBeLessThanOrEqual(width);
      await page.screenshot({
        path: `../../logs/test-runs/playwright/users-access-${theme}-${width}.png`,
        fullPage: false,
      });

      await page.getByRole("button", { name: "Add user" }).click();
      const dialog = page.getByRole("dialog", { name: "Add user" });
      await expect(dialog).toBeVisible();
      await waitForAnimations(dialog);
      const dialogBox = await dialog.boundingBox();
      expect(dialogBox).not.toBeNull();
      expect(dialogBox?.width || 0).toBeLessThanOrEqual(Math.min(600, width));
      expect(dialogBox?.height || 0).toBeLessThanOrEqual(page.viewportSize()?.height || 900);
      await expect(dialog.getByLabel("Display name")).toBeVisible();
      await expect(dialog.getByLabel("Display name")).toHaveAttribute("required", "");
      await expect(dialog.getByLabel("Email address")).toHaveAttribute("required", "");
      await expect(dialog.locator('input[name="temporary-password"]')).toHaveAttribute("autocomplete", "off");
      await expect(dialog.locator('input[name="temporary-password"]')).toHaveAttribute("required", "");
      await expect(dialog.getByRole("combobox", { name: "Account memberships" })).toHaveCount(0);
      await expect(dialog.getByRole("combobox", { name: "Primary account" })).toHaveCount(0);
      await expect(dialog.getByLabel("Confirm temporary password")).toHaveCount(0);
      await expect(dialog.getByText("Require password change", { exact: true })).toBeVisible();
      const dialogMetrics = await dialog.evaluate((element) => {
        const body = element.querySelector(".fs-dialog-body") as HTMLElement;
        const input = element.querySelector(".fs-input") as HTMLElement;
        const checkbox = element.querySelector(".fs-checkbox-mark") as HTMLElement;
        const styles = getComputedStyle(element);
        return {
          width: styles.width,
          radius: styles.borderRadius,
          bodyGap: getComputedStyle(body).gap,
          bodyPadding: getComputedStyle(body).padding,
          inputHeight: getComputedStyle(input).height,
          inputRadius: getComputedStyle(input).borderRadius,
          checkboxSize: getComputedStyle(checkbox).width,
        };
      });
      expect(dialogMetrics.radius).toBe("12px");
      expect(dialogMetrics.bodyGap).toBe("16px");
      expect(dialogMetrics.inputHeight).toBe("32px");
      expect(dialogMetrics.inputRadius).toBe("6px");
      expect(dialogMetrics.checkboxSize).toBe("16px");
      if (width > 720) {
        expect(dialogMetrics.width).toBe("600px");
        expect(dialogMetrics.bodyPadding).toBe("16px 24px");
      }
      await page.screenshot({
        path: `../../logs/test-runs/playwright/users-add-dialog-${theme}-${width}.png`,
        fullPage: false,
      });
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();

      await page.goto("/app/settings/account?tab=signin");
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await expect(page.getByRole("heading", { name: "Password" })).toBeVisible();
      const designSystemContract = await page.evaluate(() => {
        const header = document.querySelector(".fs-page-header");
        const tabList = document.querySelector(".fs-tabs-list");
        const tabPanel = document.querySelector(".fs-tabs-panel");
        if (!header || !tabList || !tabPanel) return null;
        const headerStyles = getComputedStyle(header);
        return {
          space200: headerStyles.getPropertyValue("--fs-space-200").trim(),
          headerPadding: headerStyles.padding,
          panelOffset: tabPanel.getBoundingClientRect().top - tabList.getBoundingClientRect().bottom,
        };
      });
      expect(designSystemContract).toEqual({ space200: "16px", headerPadding: "16px", panelOffset: 0 });
      const shellGeometry = await page.evaluate(() => {
        const sidebar = document.querySelector(".fs-shell-sidebar-frame")?.getBoundingClientRect();
        const main = document.querySelector("main")?.getBoundingClientRect();
        const detail = document.querySelector(".fs-settings-detail")?.getBoundingClientRect();
        return {
          sidebar: sidebar ? { left: sidebar.left, right: sidebar.right, width: sidebar.width } : null,
          main: main ? { left: main.left, right: main.right, width: main.width } : null,
          detail: detail ? { left: detail.left, right: detail.right, width: detail.width } : null,
        };
      });
      expect(shellGeometry.main).not.toBeNull();
      expect(shellGeometry.detail).not.toBeNull();
      if (width >= 768) {
        expect(shellGeometry.sidebar).not.toBeNull();
        expect(shellGeometry.main?.left || 0).toBeGreaterThanOrEqual(shellGeometry.sidebar?.right || 0);
      }
      expect(shellGeometry.detail?.left || 0).toBeGreaterThanOrEqual(shellGeometry.main?.left || 0);
      expect(shellGeometry.detail?.right || 0).toBeLessThanOrEqual(shellGeometry.main?.right || width);
      const settings = await page.locator(".fs-settings-detail").boundingBox();
      expect(settings).not.toBeNull();
      expect(settings?.x || 0).toBeGreaterThanOrEqual(0);
      expect((settings?.x || 0) + (settings?.width || 0)).toBeLessThanOrEqual(width);
      await page.screenshot({
        path: `../../logs/test-runs/playwright/account-signin-${theme}-${width}.png`,
        fullPage: false,
      });
    });
  }
}
