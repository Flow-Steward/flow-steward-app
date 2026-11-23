/** Words for the operator form editor (ADR 0075): widgets, data kinds, condition operators. */
import type { I18nContextValue } from "@/lib/i18n/context";

type T = I18nContextValue["t"];

export function widgetLabel(widget: string, t: T): string {
  const labels: Record<string, string> = {
    text: t("react.workflows.operator.widget_text", "Text"),
    textarea: t("react.workflows.operator.widget_textarea", "Text area"),
    wysiwyg: t("react.workflows.operator.widget_wysiwyg", "Formatted text editor"),
    email: t("react.workflows.operator.widget_email", "Email"),
    url: t("react.workflows.operator.widget_url", "URL"),
    date: t("react.workflows.operator.widget_date", "Date picker"),
    date_time: t("react.workflows.operator.widget_date_time", "Date and time picker"),
    time: t("react.workflows.operator.widget_time", "Time picker"),
    select: t("react.workflows.operator.widget_select", "Select"),
    radio: t("react.workflows.operator.widget_radio", "Radio buttons"),
    button_group: t("react.workflows.operator.widget_button_group", "Button group"),
    checkbox: t("react.workflows.operator.widget_checkbox", "Checkboxes"),
    multiselect: t("react.workflows.operator.widget_multiselect", "Select with several values"),
    number: t("react.workflows.operator.widget_number", "Number"),
    true_false: t("react.workflows.operator.widget_true_false", "True / False"),
    file: t("react.workflows.operator.widget_file", "File upload"),
    files: t("react.workflows.operator.widget_files", "Upload of several files"),
    group: t("react.workflows.operator.widget_group", "Group"),
    repeater: t("react.workflows.operator.widget_repeater", "Repeater"),
  };
  return labels[widget] ?? widget;
}

export function widgetDescription(widget: string, t: T): string {
  const descriptions: Record<string, string> = {
    text: t("react.workflows.operator.widget_text_help", "One line of text."),
    textarea: t("react.workflows.operator.widget_textarea_help", "Several lines of plain text."),
    wysiwyg: t("react.workflows.operator.widget_wysiwyg_help", "Text with headings, lists and links (Markdown)."),
    email: t("react.workflows.operator.widget_email_help", "An email address, checked by the browser."),
    url: t("react.workflows.operator.widget_url_help", "A web address."),
    date: t("react.workflows.operator.widget_date_help", "A calendar to pick a day."),
    date_time: t("react.workflows.operator.widget_date_time_help", "A day and a time."),
    time: t("react.workflows.operator.widget_time_help", "A time of day."),
    select: t("react.workflows.operator.widget_select_help", "A drop-down list of the options."),
    radio: t("react.workflows.operator.widget_radio_help", "All options in view, one to choose."),
    button_group: t("react.workflows.operator.widget_button_group_help", "The options as a row of buttons."),
    checkbox: t("react.workflows.operator.widget_checkbox_help", "A checkbox per option."),
    multiselect: t("react.workflows.operator.widget_multiselect_help", "A searchable list that takes several options."),
    number: t("react.workflows.operator.widget_number_help", "A number in the operator's locale."),
    true_false: t("react.workflows.operator.widget_true_false_help", "A switch."),
    file: t("react.workflows.operator.widget_file_help", "One file."),
    files: t("react.workflows.operator.widget_files_help", "Several files in upload order."),
    group: t("react.workflows.operator.widget_group_help", "The sub-fields together."),
    repeater: t("react.workflows.operator.widget_repeater_help", "Rows of sub-fields; the operator adds and removes rows."),
  };
  return descriptions[widget] ?? "";
}

export function kindLabel(kind: string, t: T): string {
  const labels: Record<string, string> = {
    string: t("react.workflows.operator.kind_string", "Text"),
    select: t("react.workflows.operator.kind_select", "Choice"),
    select_list: t("react.workflows.operator.kind_select_list", "Several choices"),
    number: t("react.workflows.operator.kind_number", "Number"),
    boolean: t("react.workflows.operator.kind_boolean", "Yes / no"),
    file: t("react.workflows.operator.kind_file", "File"),
    file_list: t("react.workflows.operator.kind_file_list", "Files"),
    object: t("react.workflows.operator.kind_object", "Group"),
    object_list: t("react.workflows.operator.kind_object_list", "List of groups"),
  };
  return labels[kind] ?? t("react.workflows.operator.kind_unsupported", "Not supported by the form");
}

export function operatorLabel(operator: string, t: T): string {
  const labels: Record<string, string> = {
    "==": t("react.workflows.operator.condition_equals", "is equal to"),
    "!=": t("react.workflows.operator.condition_not_equals", "is not equal to"),
    ">": t("react.workflows.operator.condition_greater", "is greater than"),
    "<": t("react.workflows.operator.condition_less", "is less than"),
    "==contains": t("react.workflows.operator.condition_contains", "contains"),
    "!=contains": t("react.workflows.operator.condition_not_contains", "does not contain"),
    "==pattern": t("react.workflows.operator.condition_pattern", "matches pattern"),
    "==empty": t("react.workflows.operator.condition_empty", "has no value"),
    "!=empty": t("react.workflows.operator.condition_not_empty", "has any value"),
  };
  return labels[operator] ?? operator;
}

