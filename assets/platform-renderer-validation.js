/* ── Allowed component types (closed set v1) ── */
const FS_COMPONENT_TYPES = new Set([
  'section', 'columns', 'stack', 'action_bar',
  'form', 'fieldset', 'repeater', 'key_value', 'json_editor', 'secret_field',
  'grid', 'stats_row', 'key_value_card', 'markdown', 'health_panel', 'log_list', 'empty_state',
  'alert', 'confirm_action', 'drawer_detail', 'status_badge', 'filter_bar'
]);

/* ── Allowed form widgets (closed set v1) ── */
const FS_FORM_WIDGETS = new Set([
  'text', 'textarea', 'url', 'email', 'number', 'integer', 'boolean',
  'select', 'radio_group', 'checkbox_group', 'tag_picker',
  'secret', 'json', 'key_value', 'repeater',
  'event_selector', 'project_selector', 'workflow_selector', 'provider_selector'
]);

/* ── Allowed grid column types ── */
const FS_COLUMN_TYPES = new Set([
  'text', 'datetime', 'date', 'number', 'badge', 'boolean', 'json_summary', 'link', 'id'
]);

function collectValidationErrors(manifest) {
  const errors = [...(manifest.validation_errors || [])];
  const ui = manifest.ui_manifest;
  if (!ui) {
    errors.push({ severity: 'error', message: 'Missing ui_manifest', code: 'MISSING_UI_MANIFEST' });
    return errors;
  }
  if (!ui.pages || !Array.isArray(ui.pages)) {
    errors.push({ severity: 'error', message: 'ui_manifest.pages must be an array', code: 'INVALID_PAGES' });
    return errors;
  }
  return errors;
}

function validatePages(pages, errors) {
  const pageIds = new Set();
  for (const page of pages) {
    if (pageIds.has(page.page_id)) {
      errors.push({ severity: 'error', message: `Duplicate page_id: '${page.page_id}'`, code: 'DUPLICATE_PAGE_ID' });
    }
    pageIds.add(page.page_id);
    validatePageComponents(page, errors);
  }
}

function validatePageComponents(page, errors) {
  const compIds = new Set();
  for (const comp of (page.components || [])) {
    if (compIds.has(comp.component_id)) {
      errors.push({ severity: 'error', message: `Duplicate component_id: '${comp.component_id}' in page '${page.page_id}'`, code: 'DUPLICATE_COMPONENT_ID' });
    }
    compIds.add(comp.component_id);
    if (!FS_COMPONENT_TYPES.has(comp.type)) {
      errors.push({ severity: 'error', message: `Unknown component type: '${comp.type}' in component '${comp.component_id}'`, code: 'UNKNOWN_COMPONENT_TYPE' });
    }
    if (comp.type === 'form' && comp.ui_schema) {
      validateFormUISchema(comp, errors);
    }
    if (comp.type === 'grid') {
      validateGridDef(comp, errors);
    }
  }
}

function validateFormUISchema(comp, errors) {
  const ui = comp.ui_schema;
  const fieldIds = new Set((ui.fields || []).map(f => f.field_id));
  const sectionIds = new Set((ui.sections || []).map(s => s.section_id));

  for (const field of (ui.fields || [])) {
    if (!FS_FORM_WIDGETS.has(field.widget)) {
      errors.push({ severity: 'error', message: `Unknown widget '${field.widget}' for field '${field.field_id}' in component '${comp.component_id}'`, code: 'UNKNOWN_WIDGET' });
    }
    if (field.section_id && !sectionIds.has(field.section_id)) {
      errors.push({ severity: 'warning', message: `Field '${field.field_id}' references unknown section '${field.section_id}'`, code: 'UNKNOWN_SECTION_REF' });
    }
  }

  for (const section of (ui.sections || [])) {
    for (const fid of (section.field_ids || [])) {
      if (!fieldIds.has(fid)) {
        errors.push({ severity: 'error', message: `Section '${section.section_id}' references unknown field '${fid}'`, code: 'FIELD_NOT_FOUND' });
      }
    }
  }
}

function validateGridDef(comp, errors) {
  for (const col of (comp.columns || [])) {
    if (!FS_COLUMN_TYPES.has(col.type)) {
      errors.push({ severity: 'error', message: `Unknown column type '${col.type}' for column '${col.key}' in grid '${comp.component_id}'`, code: 'UNKNOWN_COLUMN_TYPE' });
    }
  }
}

function validateRendererManifest(manifest) {
  const errors = collectValidationErrors(manifest);
  const ui = manifest.ui_manifest;
  if (!ui) {return errors;}
  if (!ui.pages || !Array.isArray(ui.pages)) {return errors;}
  validatePages(ui.pages, errors);
  return errors;
}

export {
  FS_COLUMN_TYPES,
  FS_COMPONENT_TYPES,
  FS_FORM_WIDGETS,
  validateRendererManifest,
};
