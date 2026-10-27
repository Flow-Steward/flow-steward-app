import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useId, useRef, useState } from "react";

import { cn } from "@/lib/utils/cn";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type SearchableSelectOption = {
  description?: string;
  label: string;
  value: string;
};

type SearchableSelectProps = {
  ariaLabel: string;
  className?: string;
  disabled?: boolean;
  emptyLabel: string;
  emptySearchMessage: string;
  id?: string;
  onChange: (value: string) => void;
  options: SearchableSelectOption[];
  placeholder: string;
  searchPlaceholder: string;
  value: string;
};

export function SearchableSelect({
  ariaLabel,
  className,
  disabled = false,
  emptyLabel,
  emptySearchMessage,
  id,
  onChange,
  options,
  placeholder,
  searchPlaceholder,
  value,
}: Readonly<SearchableSelectProps>) {
  const { t } = useI18n();
  const listboxId = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) {
      return undefined;
    }

    function handlePointerDown(event: MouseEvent) {
      const target = event.target;
      if (!(target instanceof Node)) {
        return;
      }
      if (rootRef.current?.contains(target)) {
        return;
      }
      setQuery("");
      setOpen(false);
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setQuery("");
        setOpen(false);
      }
    }

    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  const selected = options.find((option) => option.value === value);
  const filteredOptions = options.filter((option) => {
    const needle = query.trim().toLowerCase();
    if (!needle) {
      return true;
    }
    return `${option.label} ${option.value} ${option.description || ""}`
      .toLowerCase()
      .includes(needle);
  });

  return (
    <div className={cn("relative", open ? "z-50" : "")} ref={rootRef}>
      <button
        aria-controls={listboxId}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={ariaLabel}
        className={cn(
          "relative w-full rounded-xl border border-white/10 bg-slate-900/70 px-3 py-2 pr-9 text-left text-sm text-white outline-none transition focus:border-cyan-300/40 disabled:cursor-not-allowed disabled:opacity-60",
          className,
        )}
        disabled={disabled}
        id={id}
        onClick={() =>
          setOpen((current) => {
            const next = !current;
            if (!next) {
              setQuery("");
            }
            return next;
          })
        }
        role="combobox"
        type="button"
      >
        <span className="block min-w-0 truncate">
          {selected?.label || value || placeholder}
        </span>
        <span
          aria-hidden="true"
          className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-[10px] text-slate-500"
        >
          {open ? "^" : "v"}
        </span>
      </button>
      {open && !disabled ? (
        <div className="absolute left-0 right-0 z-50 mt-2 rounded-xl border border-white/10 bg-slate-950/98 p-2 shadow-2xl shadow-black/50 backdrop-blur">
          <input
            aria-label={t("react.shared.value1_search", "{value1} Search", { value1: String(ariaLabel) })}
            className="mb-2 w-full rounded-lg border border-white/10 bg-slate-900/70 px-2 py-2 text-sm text-white outline-none focus:border-cyan-300/45"
            onChange={(event) => setQuery(event.target.value)}
            placeholder={searchPlaceholder}
            type="text"
            value={query}
          />
          <div
            className="max-h-56 overflow-y-auto rounded-lg border border-white/10 bg-slate-950/70"
            id={listboxId}
            role="listbox"
          >
            {filteredOptions.length ? (
              filteredOptions.map((option) => (
                <button
                  aria-selected={option.value === value}
                  className={[
                    "flex w-full flex-col items-start gap-1 border-b border-white/5 px-3 py-2 text-left transition last:border-b-0 hover:bg-white/5",
                    option.value === value ? "bg-cyan-500/15" : "",
                  ].map((value) => stringFromUnknownValue(value)).join(" ")}
                  key={option.value}
                  onClick={() => {
                    onChange(option.value);
                    setQuery("");
                    setOpen(false);
                  }}
                  role="option"
                  type="button"
                >
                  <span className="text-sm text-slate-100">{option.label}</span>
                  <span className="text-xs text-slate-500">{option.value}</span>
                  {option.description ? (
                    <span className="text-xs text-slate-400">{option.description}</span>
                  ) : null}
                </button>
              ))
            ) : (
              <div className="px-3 py-3 text-sm text-slate-400">{emptySearchMessage}</div>
            )}
            {!filteredOptions.length && !query.trim() ? (
              <div className="sr-only">{emptyLabel}</div>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
