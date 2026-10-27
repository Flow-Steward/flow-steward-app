import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { ArrowRight, Check, ChevronDown } from "lucide-react";

import { DsPopover, type PopoverTriggerProps } from "@/components/shared/popover";
import {
  PERIOD_PRESETS,
  periodLabel,
  type PeriodPreset,
  type PeriodValue,
} from "@/lib/time/period";

import "@/components/shared/period-filter.css";

function moveBetweenOptions(event: KeyboardEvent<HTMLButtonElement>) {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  const group = event.currentTarget.parentElement;
  const radios = Array.from(group?.querySelectorAll<HTMLButtonElement>('[role="radio"]') ?? []);
  const index = radios.indexOf(event.currentTarget);
  event.preventDefault();
  const step = event.key === "ArrowDown" ? 1 : -1;
  radios[(index + step + radios.length) % radios.length].focus();
}

function renderPeriodFilterTrigger(
  props: PopoverTriggerProps,
  ariaLabel: string,
  label: string,
) {
  return (
    <button {...props} aria-label={`${ariaLabel}: ${label}`} className="fs-select-trigger" type="button">
      <span className="fs-select-value">{label}</span>
      <ChevronDown aria-hidden="true" className="fs-select-chevron" />
    </button>
  );
}

/**
 * The canon period filter (03_components/forms/date-field.md, "Period filter"):
 * a trigger plus a Popover holding the presets and a custom range. A preset
 * applies on click and closes; a custom range needs Apply, because a
 * half-entered pair is not a value yet. The custom branch opens inside the same
 * popover, never as a second layer.
 */
export function PeriodFilter({
  allLabel: suppliedAllLabel,
  ariaLabel: suppliedAriaLabel,
  onChange,
  value,
}: Readonly<{
  allLabel?: string;
  ariaLabel?: string;
  onChange: (value: PeriodValue) => void;
  value: PeriodValue;
}>) {
  const { t, locale } = useI18n();
  const allLabel = suppliedAllLabel ?? t("react.monitoring.all_events", "All events");
  const ariaLabel = suppliedAriaLabel ?? t("react.shared.period", "Period");
  const presetLabels: Record<PeriodPreset, string> = {
    today: t("react.shared.period_today", "Today"),
    last7: t("react.dashboard.last_7_days", "Last 7 days"),
    last30: t("react.dashboard.last_30_days", "Last 30 days"),
  };
  const [open, setOpen] = useState(false);
  const [customOpen, setCustomOpen] = useState(false);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [error, setError] = useState("");
  const [pairInvalid, setPairInvalid] = useState(false);
  const fromRef = useRef<HTMLInputElement>(null);
  const helperId = useId();
  const focusFromOnOpen = useRef(false);
  const label = periodLabel(value, allLabel, locale, presetLabels);

  useEffect(() => {
    if (customOpen && focusFromOnOpen.current) {
      focusFromOnOpen.current = false;
      fromRef.current?.focus();
    }
  }, [customOpen]);

  function changeOpen(next: boolean) {
    if (next) {
      // Reopening shows the range that is applied, not a stale draft.
      const custom = value.kind === "custom";
      setCustomOpen(custom);
      setFrom(custom ? value.from : "");
      setTo(custom ? value.to : "");
      setError("");
      setPairInvalid(false);
    }
    setOpen(next);
  }

  function choose(next: PeriodValue) {
    onChange(next);
    setOpen(false);
  }

  function openCustom() {
    focusFromOnOpen.current = true;
    setCustomOpen(true);
    if (customOpen) fromRef.current?.focus();
  }

  function apply() {
    // Apply stays enabled and says why it did nothing, rather than going grey.
    if (!from || !to) {
      setError(t("react.shared.choose_both_the_first_and_the_last_day", "Choose both the first and the last day."));
      setPairInvalid(false);
      return;
    }
    if (from > to) {
      setError(t("react.shared.the_end_of_the_window_may_not_fall_before_its_start", "The end of the window may not fall before its start."));
      setPairInvalid(true);
      return;
    }
    choose({ kind: "custom", from, to });
  }

  function editBound(setter: (next: string) => void, next: string) {
    setter(next);
    setError("");
    setPairInvalid(false);
  }

  type OptionKey = "all" | PeriodPreset | "custom";
  const options: { key: OptionKey; label: string; checked: boolean }[] = [
    { key: "all", label: allLabel, checked: value.kind === "all" },
    ...PERIOD_PRESETS.map((preset) => ({
      key: preset,
      label: presetLabels[preset],
      checked: value.kind === "preset" && value.preset === preset,
    })),
    { key: "custom", label: t("react.shared.custom_range", "Custom range"), checked: value.kind === "custom" },
  ];

  function select(key: OptionKey) {
    if (key === "custom") openCustom();
    else if (key === "all") choose({ kind: "all" });
    else choose({ kind: "preset", preset: key });
  }

  return (
    <div className="fs-period-filter">
      <DsPopover
        label={ariaLabel}
        onOpenChange={changeOpen}
        open={open}
        trigger={(props) => renderPeriodFilterTrigger(props, ariaLabel, label)}
        wide
      >
        <div aria-label={ariaLabel} role="radiogroup">
          {options.map((option) => (
            <button
              aria-checked={option.checked}
              className="fs-select-option fs-period-option"
              data-popover-initial-focus={option.checked ? "" : undefined}
              key={option.key}
              onClick={() => select(option.key)}
              onKeyDown={moveBetweenOptions}
              role="radio"
              type="button"
            >
              <span className="fs-select-option-title">{option.label}</span>
              <Check aria-hidden="true" className="fs-select-check" />
            </button>
          ))}
        </div>
        {customOpen ? (
          <div className="fs-period-custom">
            <div aria-label={t("react.shared.custom_range", "Custom range")} className="fs-period-bounds" role="group">
              <input
                aria-describedby={helperId}
                aria-invalid={pairInvalid || undefined}
                aria-label={t("react.shared.custom_range_first_day", "Custom range, first day")}
                className="fs-input"
                max={to || undefined}
                onChange={(event) => editBound(setFrom, event.target.value)}
                ref={fromRef}
                type="date"
                value={from}
              />
              <ArrowRight aria-hidden="true" className="fs-period-arrow" />
              <input
                aria-describedby={helperId}
                aria-invalid={pairInvalid || undefined}
                aria-label={t("react.shared.custom_range_last_day", "Custom range, last day")}
                className="fs-input"
                min={from || undefined}
                onChange={(event) => editBound(setTo, event.target.value)}
                type="date"
                value={to}
              />
            </div>
            <p
              className={error ? "fs-field-helper error" : "fs-field-helper"}
              id={helperId}
              role={error ? "alert" : undefined}
            >
              {error || t("react.shared.period_whole_days_help", "Whole days, both ends included.")}
            </p>
            <div className="fs-period-actions">
              <button className="fs-button" onClick={() => setOpen(false)} type="button">
                {t("common.cancel", "Cancel")} </button>
              <button className="fs-button primary" onClick={apply} type="button">
                {t("settings.common.apply", "Apply")} </button>
            </div>
          </div>
        ) : null}
      </DsPopover>
    </div>
  );
}
