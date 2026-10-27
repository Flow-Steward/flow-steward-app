/**
 * Extension-provided translations for the extension pages.
 *
 * An extension ships flat dictionaries (`locales/<code>.yaml`) that reach the
 * browser as `locale_bundles` on its compiled manifest. Text is resolved as:
 * a non-empty string in the user's language, then a non-empty `en` string, then
 * the text the declaration itself carries.
 *
 * Only what the user reads is translated. Every identity the pages derive from
 * a declaration — field names, value paths, column keys, page and section ids,
 * React keys, a saved connection's name — keeps reading the original
 * declaration through `sourceDeclaration`, so older declarations that fall back
 * to their label or title for an id keep the same id in every language.
 */
import { listFromUnknown, objectOrEmpty } from "@/pages/extensions/extension-manifest-utils";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type LocaleBundles = Record<string, Record<string, string>>;

type Declaration = Record<string, unknown>;

const DEFAULT_LOCALE = "en";
const SOURCES = new WeakMap<object, Declaration>();

function text(value: unknown): string {
  return stringFromUnknownValue(value ?? "").trim();
}

export function localeBundlesFrom(manifest: unknown): LocaleBundles {
  const root = objectOrEmpty(manifest);
  const raw = objectOrEmpty(root.locale_bundles ?? objectOrEmpty(root.compiled_manifest).locale_bundles);
  const bundles: LocaleBundles = {};
  for (const [code, entries] of Object.entries(raw)) {
    const dictionary: Record<string, string> = {};
    for (const [key, value] of Object.entries(objectOrEmpty(entries))) {
      if (typeof value === "string") dictionary[key] = value;
    }
    bundles[code] = dictionary;
  }
  return bundles;
}

/** The user's language, then English, then the declaration's own text. */
export function resolveExtensionText(
  bundles: LocaleBundles,
  locale: string,
  key: string,
  fallback: string,
): string {
  for (const code of [locale, DEFAULT_LOCALE]) {
    const value = bundles[code]?.[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return fallback;
}

/** The declaration a localized copy was made from, or the value itself. */
export function sourceDeclaration<T extends object>(value: T): T {
  return (SOURCES.get(value) as T | undefined) ?? value;
}

/** Identity of a page: `page_id`, `id`, `key`, then its title. */
export function pageIdentity(page: Declaration): string {
  const source = sourceDeclaration(page);
  return text(source.page_id || source.id || source.key || source.title);
}

/** Identity of a component for its dictionary keys; none when it declares no id. */
export function componentIdentity(component: Declaration): string {
  const source = sourceDeclaration(component);
  return text(source.component_id || source.id || source.key);
}

/** Identity of a form field: `field_id`, `id`, `path`, then its label. */
export function fieldIdentity(field: Declaration): string {
  const source = sourceDeclaration(field);
  return text(source.field_id || source.id || source.path || source.label);
}

/** Identity of a repeater's item field: its value path. */
export function itemFieldIdentity(field: Declaration): string {
  const source = sourceDeclaration(field);
  return text(source.path || source.name || source.field_id || source.id);
}

/** The data key a grid column reads: `key`, then its label. */
export function columnIdentity(column: Declaration): string {
  const source = sourceDeclaration(column);
  return text(source.key || source.label);
}

type Translate = (key: string, fallback: unknown) => string;

function localizedCopy(source: Declaration, patch: Declaration): Declaration {
  const copy = { ...source, ...patch };
  SOURCES.set(copy, sourceDeclaration(source));
  return copy;
}

/** Set `field` on the patch when the declaration has text there to translate. */
function put(patch: Declaration, source: Declaration, field: string, translate: Translate, key: string) {
  if (text(source[field])) patch[field] = translate(key, source[field]);
}

function localizeOptions(options: unknown, prefix: string, translate: Translate): unknown {
  if (!Array.isArray(options)) return options;
  return options.map((row) => {
    if (!row || typeof row !== "object" || Array.isArray(row)) return row;
    const option = row as Declaration;
    const value = text(option.value || option.id || option.key);
    if (!value) return row;
    const patch: Declaration = {};
    put(patch, option, "label", translate, `${prefix}.option.${value}.label`);
    put(patch, option, "title", translate, `${prefix}.option.${value}.label`);
    return localizedCopy(option, patch);
  });
}

function localizeFieldText(field: Declaration, prefix: string, translate: Translate): Declaration {
  const patch: Declaration = {};
  put(patch, field, "label", translate, `${prefix}.label`);
  // Help is whichever of description/help the field uses.
  if (text(field.description)) put(patch, field, "description", translate, `${prefix}.help`);
  else put(patch, field, "help", translate, `${prefix}.help`);
  put(patch, field, "placeholder", translate, `${prefix}.placeholder`);
  if (Array.isArray(field.options)) patch.options = localizeOptions(field.options, prefix, translate);
  const props = objectOrEmpty(field.props);
  if (Object.keys(props).length) {
    const propsPatch: Declaration = {};
    if (Array.isArray(props.options)) propsPatch.options = localizeOptions(props.options, prefix, translate);
    if (Array.isArray(props.fields)) {
      propsPatch.fields = listFromUnknown(props.fields).map((item) => {
        const itemId = itemFieldIdentity(item);
        return itemId ? localizeFieldText(item, `${prefix}.field.${itemId}`, translate) : item;
      });
    }
    if (Object.keys(propsPatch).length) patch.props = { ...props, ...propsPatch };
  }
  return localizedCopy(field, patch);
}

function localizeFields(fields: unknown, componentPrefix: string, translate: Translate): unknown {
  if (!Array.isArray(fields)) return fields;
  return listFromUnknown(fields).map((field) => {
    const fieldId = fieldIdentity(field);
    return fieldId ? localizeFieldText(field, `${componentPrefix}.field.${fieldId}`, translate) : field;
  });
}

function localizeActions(actions: unknown, prefix: string, translate: Translate): unknown {
  const entries = objectOrEmpty(actions);
  if (!Object.keys(entries).length) return actions;
  const localized: Declaration = {};
  for (const [key, raw] of Object.entries(entries)) {
    if (key === "save_label" || key === "test_label") {
      localized[key] = typeof raw === "string" ? translate(`${prefix}.${key}`, raw) : raw;
      continue;
    }
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      localized[key] = raw;
      continue;
    }
    const action = raw as Declaration;
    const patch: Declaration = {};
    put(patch, action, "label", translate, `${prefix}.action.${key}.label`);
    put(patch, action, "title", translate, `${prefix}.action.${key}.label`);
    localized[key] = localizedCopy(action, patch);
  }
  return localized;
}

function localizeComponent(component: Declaration, translate: Translate): Declaration {
  const componentId = componentIdentity(component);
  if (!componentId) return component;
  const prefix = `component.${componentId}`;
  const patch: Declaration = {};
  put(patch, component, "title", translate, `${prefix}.title`);
  put(patch, component, "description", translate, `${prefix}.description`);
  put(patch, component, "content", translate, `${prefix}.content`);
  if (Array.isArray(component.fields)) patch.fields = localizeFields(component.fields, prefix, translate);
  const uiSchema = objectOrEmpty(component.ui_schema);
  if (Object.keys(uiSchema).length) {
    const schemaPatch: Declaration = {};
    put(schemaPatch, uiSchema, "description", translate, `${prefix}.description`);
    if (Array.isArray(uiSchema.fields)) schemaPatch.fields = localizeFields(uiSchema.fields, prefix, translate);
    if (Array.isArray(uiSchema.sections)) {
      schemaPatch.sections = listFromUnknown(uiSchema.sections).map((section) => {
        const sectionId = text(section.section_id);
        if (!sectionId) return section;
        const sectionPatch: Declaration = {};
        put(sectionPatch, section, "title", translate, `${prefix}.section.${sectionId}.title`);
        put(sectionPatch, section, "description", translate, `${prefix}.section.${sectionId}.description`);
        return localizedCopy(section, sectionPatch);
      });
    }
    if (uiSchema.actions !== undefined) schemaPatch.actions = localizeActions(uiSchema.actions, prefix, translate);
    patch.ui_schema = localizedCopy(uiSchema, schemaPatch);
  }
  if (Array.isArray(component.columns)) {
    patch.columns = listFromUnknown(component.columns).map((column) => {
      const columnId = columnIdentity(column);
      if (!columnId) return column;
      const columnPatch: Declaration = {};
      put(columnPatch, column, "label", translate, `${prefix}.column.${columnId}.label`);
      return localizedCopy(column, columnPatch);
    });
  }
  if (Array.isArray(component.row_actions)) {
    patch.row_actions = listFromUnknown(component.row_actions).map((action) => {
      const actionId = text(action.action_id || action.id || action.query_id);
      if (!actionId) return action;
      const actionPatch: Declaration = {};
      put(actionPatch, action, "label", translate, `${prefix}.row_action.${actionId}.label`);
      put(actionPatch, action, "title", translate, `${prefix}.row_action.${actionId}.label`);
      return localizedCopy(action, actionPatch);
    });
  }
  const emptyState = objectOrEmpty(component.empty_state);
  if (Object.keys(emptyState).length) {
    const emptyPatch: Declaration = {};
    put(emptyPatch, emptyState, "title", translate, `${prefix}.empty_state`);
    put(emptyPatch, emptyState, "description", translate, `${prefix}.empty_state_description`);
    patch.empty_state = localizedCopy(emptyState, emptyPatch);
  }
  const data = objectOrEmpty(component.data);
  if (text(data.account_label)) {
    patch.data = { ...data, account_label: translate(`${prefix}.account_label`, data.account_label) };
  }
  return localizedCopy(component, patch);
}

function localizePage(page: Declaration, translate: Translate): Declaration {
  const pageId = pageIdentity(page);
  const patch: Declaration = {};
  if (pageId) {
    put(patch, page, "title", translate, `page.${pageId}.title`);
    put(patch, page, "description", translate, `page.${pageId}.description`);
  }
  if (Array.isArray(page.components)) {
    patch.components = listFromUnknown(page.components).map((component) =>
      localizeComponent(component, translate),
    );
  }
  return localizedCopy(page, patch);
}

/** The extension's own name and description in the user's language. */
export function localizeExtensionIdentity<T extends { display_name?: string; description?: string }>(
  record: T,
  bundles: LocaleBundles,
  locale: string,
): T {
  if (!Object.keys(bundles).length) return record;
  return {
    ...record,
    display_name: resolveExtensionText(bundles, locale, "ext.display_name", text(record.display_name)),
    description: resolveExtensionText(bundles, locale, "ext.description", text(record.description)),
  };
}

function localizeOauthProviders(providers: unknown, translate: Translate): unknown {
  if (!Array.isArray(providers)) return providers;
  return listFromUnknown(providers).map((provider) => {
    const providerId = text(provider.provider_id);
    if (!providerId) return provider;
    const prefix = `oauth.${providerId}`;
    const patch: Declaration = {};
    put(patch, provider, "display_name", translate, `${prefix}.display_name`);
    put(patch, provider, "description", translate, `${prefix}.description`);
    if (Array.isArray(provider.credential_fields)) {
      patch.credential_fields = listFromUnknown(provider.credential_fields).map((field) => {
        const fieldId = text(field.field_id);
        if (!fieldId) return field;
        const fieldPatch: Declaration = {};
        const fieldPrefix = `${prefix}.credential.${fieldId}`;
        put(fieldPatch, field, "label", translate, `${fieldPrefix}.label`);
        put(fieldPatch, field, "help_text", translate, `${fieldPrefix}.help_text`);
        // A secret field never shows a placeholder, whatever a dictionary says.
        if (field.secret !== true) put(fieldPatch, field, "placeholder", translate, `${fieldPrefix}.placeholder`);
        return localizedCopy(field, fieldPatch);
      });
    }
    return localizedCopy(provider, patch);
  });
}

function localizeConnectionTypes(connectionTypes: unknown, translate: Translate): unknown {
  if (!Array.isArray(connectionTypes)) return connectionTypes;
  return listFromUnknown(connectionTypes).map((row) => {
    const typeId = text(row.connection_type_id);
    if (!typeId) return row;
    const patch: Declaration = {};
    put(patch, row, "display_name", translate, `connection.${typeId}.display_name`);
    put(patch, row, "description", translate, `connection.${typeId}.description`);
    return localizedCopy(row, patch);
  });
}

function localizeUiManifest(uiManifest: unknown, translate: Translate): unknown {
  const ui = objectOrEmpty(uiManifest);
  if (!Array.isArray(ui.pages)) return uiManifest;
  return { ...ui, pages: listFromUnknown(ui.pages).map((page) => localizePage(page, translate)) };
}

/**
 * A copy of an extension's compiled manifest whose readable text is in the
 * user's language: its name, pages, OAuth setup copy and connection types.
 * Ids, schemas, actions and every other field are the manifest's own.
 */
export function localizeExtensionManifest(manifest: unknown, locale: string): unknown {
  const root = objectOrEmpty(manifest);
  const bundles = localeBundlesFrom(root);
  if (!Object.keys(bundles).length) return manifest;
  const translate: Translate = (key, fallback) =>
    resolveExtensionText(bundles, locale, key, text(fallback));
  const localized: Declaration = { ...root };
  put(localized, root, "display_name", translate, "ext.display_name");
  put(localized, root, "description", translate, "ext.description");
  if (root.ui_manifest !== undefined) localized.ui_manifest = localizeUiManifest(root.ui_manifest, translate);
  if (root.oauth_providers !== undefined) {
    localized.oauth_providers = localizeOauthProviders(root.oauth_providers, translate);
  }
  if (root.connection_types !== undefined) {
    localized.connection_types = localizeConnectionTypes(root.connection_types, translate);
  }
  const compiled = objectOrEmpty(root.compiled_manifest);
  if (Object.keys(compiled).length && compiled.ui_manifest !== undefined) {
    localized.compiled_manifest = { ...compiled, ui_manifest: localizeUiManifest(compiled.ui_manifest, translate) };
  }
  return localized;
}
