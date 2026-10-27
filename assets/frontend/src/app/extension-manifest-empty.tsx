import type { ComponentType } from "react";

import type { RegisteredPageComponentProps } from "@/app/extensions";

export const accountProfileTabs = [];
export const registeredPageComponents: Record<string, ComponentType<RegisteredPageComponentProps>> = {};

export default {
  accountProfileTabs,
  registeredPageComponents,
};
