import {
  columnIdentity,
  fieldIdentity,
  localizeExtensionManifest,
  pageIdentity,
  resolveExtensionText,
  sourceDeclaration,
} from "@/pages/extensions/extension-locale";
import { manifestFieldName } from "@/pages/extensions/extension-manifest-fields";

type Row = Record<string, unknown>;

function manifest(bundles: Row): Row {
  return {
    display_name: "Shop",
    locale_bundles: bundles,
    ui_manifest: {
      pages: [
        {
          title: "Orders",
          components: [
            {
              component_id: "orders",
              type: "grid",
              title: "Orders",
              columns: [{ label: "Total" }, { key: "status", label: "Status" }],
              empty_state: { title: "No orders", description: "Nothing yet" },
              ui_schema: {
                fields: [
                  {
                    label: "Region",
                    widget: "select",
                    options: [{ value: "eu", label: "Europe" }],
                  },
                  {
                    field_id: "contacts",
                    widget: "repeater",
                    label: "Contacts",
                    props: { fields: [{ name: "email", label: "Email", placeholder: "you@shop" }] },
                  },
                ],
              },
            },
          ],
        },
      ],
    },
    oauth_providers: [
      {
        provider_id: "shop",
        display_name: "Shop OAuth",
        credential_fields: [
          { field_id: "client_secret", label: "Secret", help_text: "From the app", secret: true, placeholder: "" },
          { field_id: "client_id", label: "Client ID", help_text: "From the app", secret: false, placeholder: "abc" },
        ],
      },
    ],
    connection_types: [{ connection_type_id: "shop_api", display_name: "Shop API" }],
  };
}

const LT = {
  "page.Orders.title": "Užsakymai",
  "component.orders.column.Total.label": "Suma",
  "component.orders.column.status.label": "Būsena",
  "component.orders.empty_state": "Užsakymų nėra",
  "component.orders.field.Region.label": "Regionas",
  "component.orders.field.Region.option.eu.label": "Europa",
  "component.orders.field.contacts.field.email.label": "El. paštas",
  "component.orders.field.contacts.field.email.placeholder": "jus@parduotuve",
  "oauth.shop.display_name": "Parduotuvės OAuth",
  "oauth.shop.credential.client_secret.placeholder": "should never show",
  "oauth.shop.credential.client_id.placeholder": "abc-lt",
  "connection.shop_api.display_name": "Parduotuvės API",
};

function localizedPage(locale: string) {
  const localized = localizeExtensionManifest(manifest({ en: {}, lt: LT }), locale) as Row;
  const pages = (localized.ui_manifest as Row).pages as Row[];
  return { localized, page: pages[0], component: (pages[0].components as Row[])[0] };
}

describe("extension text resolution", () => {
  it("prefers the language, then English, then the declaration", () => {
    const bundles = { lt: { a: "lt", b: " " }, en: { a: "en", b: "en-b", c: "" } };
    expect(resolveExtensionText(bundles, "lt", "a", "src")).toBe("lt");
    expect(resolveExtensionText(bundles, "lt", "b", "src")).toBe("en-b");
    expect(resolveExtensionText(bundles, "lt", "c", "src")).toBe("src");
    expect(resolveExtensionText(bundles, "fr", "a", "src")).toBe("en");
    expect(resolveExtensionText({}, "lt", "a", "src")).toBe("src");
  });

  it("translates what the user reads and keeps every id on the declaration", () => {
    const { page, component } = localizedPage("lt");
    const columns = component.columns as Row[];
    const [region, contacts] = (component.ui_schema as Row).fields as Row[];

    expect(page.title).toBe("Užsakymai");
    expect(pageIdentity(page)).toBe("Orders");
    expect(columns.map((column) => column.label)).toEqual(["Suma", "Būsena"]);
    expect(columns.map(columnIdentity)).toEqual(["Total", "status"]);
    expect((component.empty_state as Row).title).toBe("Užsakymų nėra");
    expect((component.empty_state as Row).description).toBe("Nothing yet");
    expect(region.label).toBe("Regionas");
    expect(fieldIdentity(region)).toBe("Region");
    expect(manifestFieldName(region)).toBe("Region");
    expect((region.options as Row[])[0]).toMatchObject({ value: "eu", label: "Europa" });
    const email = ((contacts.props as Row).fields as Row[])[0];
    expect(email).toMatchObject({ name: "email", label: "El. paštas", placeholder: "jus@parduotuve" });
  });

  it("localizes OAuth copy but never gives a secret field a placeholder", () => {
    const { localized } = localizedPage("lt");
    const provider = (localized.oauth_providers as Row[])[0];
    const [secret, clientId] = provider.credential_fields as Row[];

    expect(provider.display_name).toBe("Parduotuvės OAuth");
    expect(secret.placeholder).toBe("");
    expect(clientId.placeholder).toBe("abc-lt");
    expect((localized.connection_types as Row[])[0].display_name).toBe("Parduotuvės API");
  });

  it("never changes the manifest it was given", () => {
    const original = manifest({ lt: LT });
    const snapshot = JSON.stringify(original);
    const localized = localizeExtensionManifest(original, "lt") as Row;
    const page = ((localized.ui_manifest as Row).pages as Row[])[0];

    expect(JSON.stringify(original)).toBe(snapshot);
    expect(sourceDeclaration(page)).toBe(((original.ui_manifest as Row).pages as Row[])[0]);
  });

  it("leaves an extension without dictionaries untouched", () => {
    const original = { ui_manifest: { pages: [{ title: "Setup" }] } };
    expect(localizeExtensionManifest(original, "lt")).toBe(original);
  });
});
