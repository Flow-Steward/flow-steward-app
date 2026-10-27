import { Button } from "@/components/ui/button";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useRef, useState, type ReactNode } from "react";

import {
  textOrEmpty,
  workflowVariableSuggestionPanelId,
} from "@/pages/workflows/detail/detail-helpers";
import {
  templateInputRows,
  templateSegments,
  type TemplateInputRow,
} from "@/pages/workflows/detail/detail-template-core";

const STEP_KIND = "template";
function localizedFORMATS(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  { value: "text", label: t("react.workflows.text", "Text") },
  { value: "markdown", label: "Markdown" },
  { value: "html", label: "HTML" },
];
}

const INPUT_TYPES = ["text", "number", "boolean", "object", "array", "json"];
const JINJA_DOCS_URL = "https://jinja.palletsprojects.com/en/stable/templates/";

const EDITOR_TEXT =
  "m-0 whitespace-pre-wrap break-words px-3 py-2 font-mono text-[13px] leading-5";

/**
 * A plain textarea over a coloured copy of its own text. The textarea owns every
 * keystroke, the caret, selection, copy and delete; the copy underneath only
 * paints `{{ … }}` so a value stands out from the static text.
 */
export function TemplateTextEditor({
  ariaLabel,
  inputNames,
  onChange,
  value,
}: Readonly<{
  ariaLabel: string;
  inputNames: string[];
  onChange: (next: string) => void;
  value: string;
}>) {
  const { t } = useI18n();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const mirrorRef = useRef<HTMLPreElement>(null);
  const [insertChoice, setInsertChoice] = useState("");

  const insertVariable = (name: string) => {
    const field = textareaRef.current;
    const start = field ? field.selectionStart : value.length;
    const end = field ? field.selectionEnd : value.length;
    const snippet = `{{ ${name} }}`;
    onChange(`${value.slice(0, start)}${snippet}${value.slice(end)}`);
    globalThis.requestAnimationFrame?.(() => {
      if (!field) return;
      field.focus();
      field.setSelectionRange(start + snippet.length, start + snippet.length);
    });
  };

  return (
    <div className="space-y-2">
      <div className="relative overflow-hidden rounded border border-default fs-surface-inset">
        <pre aria-hidden="true" className={`${EDITOR_TEXT} min-h-40 fs-ink-primary`} ref={mirrorRef}>
          {templateSegments(value).map((segment, index) => {
            if (segment.kind === "expression") {
              return (
                <span
                  className="rounded fs-surface-selected fs-ink-brand"
                  data-template-expression="true"
                  key={`${index}-${segment.text}`}
                >
                  {segment.text}
                </span>
              );
            }
            if (segment.kind === "tag") {
              return (
                <span className="fs-ink-attention" key={`${index}-${segment.text}`}>
                  {segment.text}
                </span>
              );
            }
            return <span key={`${index}-${segment.text}`}>{segment.text}</span>;
          })}
          {/* Keeps the last empty line as tall as the textarea's. */}
          {" "}
        </pre>
        <textarea
          aria-label={ariaLabel}
          className={`${EDITOR_TEXT} absolute inset-0 h-full w-full resize-none overflow-hidden bg-transparent text-transparent caret-[var(--fs-text-primary)] outline-none selection:bg-[var(--fs-action-primary-wash)] selection:text-transparent`}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => event.stopPropagation()}
          onScroll={(event) => {
            if (mirrorRef.current) mirrorRef.current.scrollTop = event.currentTarget.scrollTop;
          }}
          ref={textareaRef}
          spellCheck={false}
          value={value}
        />
      </div>
      <label className="flex items-center gap-2 text-xs fs-ink-secondary">
        <span>{t("react.workflows.insert_variable", "Insert variable")}</span>
        <select
          aria-label={t("react.workflows.insert_variable", "Insert variable")}
          className="fs-input"
          disabled={!inputNames.length}
          onChange={(event) => {
            const name = event.target.value;
            setInsertChoice("");
            if (name) insertVariable(name);
          }}
          value={insertChoice}
        >
          <option value="">{inputNames.length ? t("react.workflows.choose_an_input", "Choose an input…") : t("react.workflows.declare_an_input_first", "Declare an input first")}</option>
          {inputNames.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

function TemplateHelp() {
  const { t } = useI18n();
  return (
    <details className="rounded-lg border border-default fs-surface-inset p-2 text-xs fs-ink-secondary">
      <summary className="cursor-pointer fs-ink-primary">{t("react.workflows.template_help", "Template help")}</summary>
      <div className="mt-2 space-y-2">
        <p>
          {t("react.workflows.the_template_is_standard_jinja_it_reads_only_the_inputs_declared_below_by_their_names", "The template is standard Jinja. It reads only the inputs declared below, by their names.")}</p>
        <pre className="whitespace-pre-wrap rounded fs-surface-inset p-2 font-mono text-[11px]">
          {[
            "Value:            {{ header }}",
            "Field:            {{ product.name }}",
            "Loop:             {% for image in images %}![{{ image.alt }}]({{ image.url }}){% endfor %}",
            'Optional value:   {{ footer | default("") }}',
            "Check:            {% if footer is defined %}…{% endif %}",
            "Literal braces:   {% raw %}{{ not a variable }}{% endraw %}",
          ].join("\n")}
        </pre>
        <p>
          {t("react.workflows.a_value_that_is_missing_fails_the_step_unless_the_template_gives_a_fallback_with", "A value that is missing fails the step, unless the template gives a fallback with")}{" "}
          <code>default(…)</code> {" "}{t("react.workflows.or_checks_it_with", "or checks it with")}{" "}<code>is defined</code>.
        </p>
        <p>
          {t("react.workflows.in_html_every_inserted_value_is_escaped_so", "In HTML every inserted value is escaped, so")}{" "}<code>&lt;</code> {" "}{t("react.workflows.and", "and")}{" "}<code>&amp;</code>{" "}
          {t("react.workflows.show_as_text", "show as text.")}{" "}<code>{"{{ fragment | safe }}"}</code> {" "}{t("react.workflows.inserts_a_fragment_as_html_use_it_only_for_markup_you_trust_because_it_reaches_the_reader_unchanged_text_and_m", "inserts a fragment as HTML: use it only for markup you trust, because it reaches the reader unchanged. Text and Markdown insert values as they are.")}</p>
        <p>
          {t("react.workflows.prepare_data_with_a_transform_step_before_the_template_the_template_only_lays_it_out", "Prepare data with a Transform step before the template; the template only lays it out.")}</p>
        <a className="fs-ink-brand underline" href={JINJA_DOCS_URL} rel="noreferrer" target="_blank">
          {t("react.workflows.jinja_template_documentation", "Jinja template documentation")}</a>
      </div>
    </details>
  );
}

export function TemplateStepSetup({
  ctx,
  idPrefix = "template",
  inputClass,
}: Readonly<{
  ctx: Record<string, any>;
  /** Keeps suggestion panels apart when several templates share a drawer (for_each rows). */
  idPrefix?: string;
  inputClass: string;
}>) {
  const { t } = useI18n();
  const { renderWorkflowVariableInput, renderWorkflowDestinationInput, replaceStepKindConfigField, stepKindConfig } = ctx;
  const config = (stepKindConfig || {}) as Record<string, unknown>;
  const rows = templateInputRows(config.inputs);
  const text = typeof config.text === "string" ? config.text : "";
  const setField = (field: string, value: unknown) =>
    replaceStepKindConfigField(STEP_KIND, field, value);
  const setRows = (next: TemplateInputRow[]) => setField("inputs", next);
  // Rows have no id of their own; each keeps a key for as long as it exists, so
  // typing a name never remounts the row it is typed into.
  const [rowKeys, setRowKeys] = useState<{ keys: string[]; issued: number }>({ keys: [], issued: 0 });
  if (rowKeys.keys.length !== rows.length) {
    const keys = rowKeys.keys.slice(0, rows.length);
    let issued = rowKeys.issued;
    while (keys.length < rows.length) {
      issued += 1;
      keys.push(`template-input-${issued}`);
    }
    setRowKeys({ keys, issued });
  }
  const updateRow = (index: number, patch: Partial<TemplateInputRow>) =>
    setRows(rows.map((row, at) => (at === index ? { ...row, ...patch } : row)));

  const field = (label: string, control: ReactNode) => (
    <label className="block text-xs fs-ink-secondary">
      <span>{label}</span>
      {control}
    </label>
  );

  return (
    <div className="space-y-3 rounded-xl border border-default fs-surface-inset p-3">
      <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">{t("react.workflows.template_variant_0575f29d", "Template")}</p>
      {field(
        t("component.json.format", "Format"),
        <select
          aria-label={t("react.workflows.template_format", "Template format")}
          className={inputClass}
          onChange={(event) => setField("format", event.target.value)}
          value={textOrEmpty(config.format) || "text"}
        >
          {localizedFORMATS(t).map((format) => (
            <option key={format.value} value={format.value}>
              {format.label}
            </option>
          ))}
        </select>,
      )}
      <div className="space-y-2">
        <p className="text-xs fs-ink-secondary">{t("registry.agent_detail.io.inputs", "Inputs")}</p>
        {rows.length ? null : (
          <p className="text-xs fs-ink-tertiary">
            {t("react.workflows.no_inputs_yet_a_template_without_inputs_is_plain_text", "No inputs yet. A template without inputs is plain text.")}</p>
        )}
        {rows.map((row, index) => (
          <div
            className="grid gap-2 rounded-lg border border-default p-2 sm:grid-cols-[minmax(0,1fr)_110px_minmax(0,1.6fr)_auto]"
            key={rowKeys.keys[index] ?? `template-input-pending-${row.name}`}
          >
            {field(
              t("settings.mcp.fields.name", "Name"),
              <input
                aria-label={t("react.workflows.input_value0_name", "Input {value0} name", { value0: index + 1 })}
                className={inputClass}
                onChange={(event) => updateRow(index, { name: event.target.value })}
                placeholder="header"
                value={row.name}
              />,
            )}
            {field(
              t("trash.columns.type", "Type"),
              <select
                aria-label={t("react.workflows.input_value0_type", "Input {value0} type", { value0: index + 1 })}
                className={inputClass}
                onChange={(event) => updateRow(index, { type: event.target.value })}
                value={row.type}
              >
                {INPUT_TYPES.map((type) => (
                  <option key={type} value={type}>
                    {type}
                  </option>
                ))}
              </select>,
            )}
            {field(
              t("react.registry.source_variant_0e570ca6", "Source"),
              renderWorkflowVariableInput({
                // A template reads workflow variables only; artifacts and
                // project settings are refused when the step is saved.
                accept: (path: string) => path.startsWith("state.") && path !== "state.",
                ariaLabel: t("react.workflows.input_value0_source", "Input {value0} source", { value0: index + 1 }),
                className: inputClass,
                onChange: (next: string) => updateRow(index, { source: next }),
                panelId: workflowVariableSuggestionPanelId(`${idPrefix}-input-${index}`),
                placeholder: "state.outputs.header",
                value: row.source,
              }),
            )}
            <div className="flex items-end gap-2 pb-1">
              <label className="flex items-center gap-1 text-xs fs-ink-secondary">
                <input
                  checked={row.required}
                  onChange={(event) => updateRow(index, { required: event.target.checked })}
                  type="checkbox"
                />
                {t("project_edit.fields.required", "Required")}</label>
              <Button
                aria-label={t("react.workflows.remove_input_value0", "Remove input {value0}", { value0: index + 1 })}
                variant="danger"
                onClick={() => {
                  setRowKeys((current) => ({
                    ...current,
                    keys: current.keys.filter((_, at) => at !== index),
                  }));
                  setRows(rows.filter((_, at) => at !== index));
                }}
                type="button"
              >
                {t("extensions.repeater.remove", "Remove")}</Button>
            </div>
          </div>
        ))}
        <Button
          variant="secondary"
          onClick={() =>
            setRows([...rows, { name: "", type: "text", required: true, source: "" }])
          }
          type="button"
        >
          {t("react.workflows.add_input", "Add input")}</Button>
      </div>
      <div className="space-y-1">
        <p className="text-xs fs-ink-secondary">{t("react.workflows.template_text", "Template text")}</p>
        <TemplateTextEditor
          ariaLabel={t("react.workflows.template_text", "Template text")}
          inputNames={rows.map((row) => row.name).filter(Boolean)}
          onChange={(next) => setField("text", next)}
          value={text}
        />
      </div>
      {field(
        t("react.workflows.save_text_to", "Save text to"),
        renderWorkflowDestinationInput({
          ariaLabel: t("react.workflows.save_text_to", "Save text to"),
          className: inputClass,
          onChange: (next: string) => setField("result_target", next),
          panelId: workflowVariableSuggestionPanelId(`${idPrefix}-result-target`),
          placeholder: "state.outputs.post_markdown",
          value: textOrEmpty(config.result_target),
        }),
      )}
      <TemplateHelp />
    </div>
  );
}
