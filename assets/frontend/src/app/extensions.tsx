import type { ComponentType } from "react";

import * as optionalFrontendExtensionManifestModule from "@flow-steward/frontend-extension-manifest";

import type { RegisteredPageRecord } from "@/lib/auth/AuthProvider";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

export type RegisteredPageComponentProps = {
  page: RegisteredPageRecord;
};

export type AccountProfileTabExtension = {
  key: string;
  label: string;
  title?: string;
  componentKey: string;
  order?: number;
};

export type AccountProfileTabManifestEntry = {
  key: string;
  label?: string;
  title?: string;
  componentKey?: string;
  component_key?: string;
  order?: number;
};

export type FrontendExtensionManifest = {
  accountProfileTabs?: AccountProfileTabManifestEntry[];
  registeredPageComponents?: Record<string, ComponentType<RegisteredPageComponentProps>>;
};

type FrontendExtensionManifestModule = FrontendExtensionManifest & {
  default?: FrontendExtensionManifest;
};

export const emptyExtensionManifest: FrontendExtensionManifest = {
  accountProfileTabs: [],
  registeredPageComponents: {},
};

function normalizeAccountProfileTab(raw: unknown): AccountProfileTabExtension | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return null;
  }
  const record = raw as Record<string, unknown>;
  const key = firstStringFromUnknownValue([record.key, ""]).trim();
  const label = firstStringFromUnknownValue([record.label, key]).trim();
  const componentKey = firstStringFromUnknownValue([record.componentKey, record.component_key, ""]).trim();
  if (!key || !label || !componentKey) {
    return null;
  }
  const orderValue = Number(record.order ?? 100);
  return {
    key,
    label,
    title: firstStringFromUnknownValue([record.title, label]).trim(),
    componentKey,
    order: Number.isFinite(orderValue) ? orderValue : 100,
  };
}

export function collectAccountProfileTabs(
  modules: Record<string, FrontendExtensionManifestModule>,
): AccountProfileTabExtension[] {
  const tabs = new Map<string, AccountProfileTabExtension>();

  Object.values(modules).forEach((module) => {
    [module.default, module].forEach((manifest) => {
      if (!Array.isArray(manifest?.accountProfileTabs)) {
        return;
      }
      manifest.accountProfileTabs.forEach((rawTab) => {
        const tab = normalizeAccountProfileTab(rawTab);
        if (tab) {
          tabs.set(tab.key, tab);
        }
      });
    });
  });

  return Array.from(tabs.values()).sort(
    (left, right) => (left.order ?? 100) - (right.order ?? 100) || left.label.localeCompare(right.label),
  );
}

export function collectRegisteredPageComponents(
  modules: Record<string, FrontendExtensionManifestModule>,
): Record<string, ComponentType<RegisteredPageComponentProps>> {
  const components: Record<string, ComponentType<RegisteredPageComponentProps>> = {};

  Object.values(modules).forEach((module) => {
    [module.default, module].forEach((manifest) => {
      if (!manifest?.registeredPageComponents) {
        return;
      }
      Object.assign(components, manifest.registeredPageComponents);
    });
  });

  return components;
}

const frontendExtensionModules: Record<string, FrontendExtensionManifestModule> = {
  optional: optionalFrontendExtensionManifestModule as FrontendExtensionManifestModule,
};

const collectedAccountProfileTabs = collectAccountProfileTabs(frontendExtensionModules);
const collectedRegisteredPageComponents = collectRegisteredPageComponents(frontendExtensionModules);

const frontendExtensionManifest: FrontendExtensionManifest = {
  accountProfileTabs: collectedAccountProfileTabs,
  registeredPageComponents: collectedRegisteredPageComponents,
};

export const registeredAccountProfileTabs: AccountProfileTabExtension[] = collectedAccountProfileTabs;
export const registeredPageComponents = collectedRegisteredPageComponents;

export function getFrontendExtensionManifest(): FrontendExtensionManifest {
  return frontendExtensionManifest;
}

export function resolveRegisteredPageComponent(componentKey: string) {
  const normalizedKey = firstStringFromUnknownValue([componentKey, ""]).trim();
  if (!normalizedKey) {
    return undefined;
  }
  return registeredPageComponents[normalizedKey];
}
