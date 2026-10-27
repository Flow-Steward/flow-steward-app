import { createElement } from "react";

import { collectAccountProfileTabs, collectRegisteredPageComponents } from "@/app/extensions";

function FixtureOne() {
  return createElement("div", null, "Fixture one");
}

function FixtureTwo() {
  return createElement("div", null, "Fixture two");
}

describe("frontend extension manifest", () => {
  it("collects registered page components from named and default exports", () => {
    const components = collectRegisteredPageComponents({
      named: {
        registeredPageComponents: {
          "fixture.one": FixtureOne,
        },
      },
      defaulted: {
        default: {
          registeredPageComponents: {
            "fixture.two": FixtureTwo,
          },
        },
      },
    });

    expect(components["fixture.one"]).toBe(FixtureOne);
    expect(components["fixture.two"]).toBe(FixtureTwo);
  });

  it("collects generic account profile tab extensions", () => {
    const tabs = collectAccountProfileTabs({
      named: {
        accountProfileTabs: [
          {
            key: "activity",
            label: "Activity",
            componentKey: "fixture.one",
            order: 20,
          },
        ],
      },
      defaulted: {
        default: {
          accountProfileTabs: [
            {
              key: "preferences",
              label: "Preferences",
              component_key: "fixture.two",
              order: 10,
            },
          ],
        },
      },
    });

    expect(tabs.map((tab) => tab.key)).toEqual(["preferences", "activity"]);
    expect(tabs[0].componentKey).toBe("fixture.two");
  });
});
