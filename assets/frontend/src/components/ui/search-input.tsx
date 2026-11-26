import { Search, X } from "lucide-react";
import { useRef, type InputHTMLAttributes, type RefObject } from "react";

import { useI18n } from "@/lib/i18n/useI18n";
import { cn } from "@/lib/utils/cn";

import "./search-input.css";

export function SearchClearButton({ onClear }: Readonly<{ onClear: () => void }>) {
  const { t } = useI18n();
  return (
    <button aria-label={t("react.shared.clear_search", "Clear search")} className="fs-search-clear" onClick={onClear} type="button">
      <X aria-hidden="true" size={16} strokeWidth={2} />
    </button>
  );
}

type SearchInputProps = Omit<InputHTMLAttributes<HTMLInputElement>, "onChange" | "type" | "value"> & {
  inputRef?: RefObject<HTMLInputElement | null>;
  onValueChange: (value: string) => void;
  value: string;
};

export function DsSearchInput({ className, inputRef, onValueChange, value, ...props }: Readonly<SearchInputProps>) {
  const internalRef = useRef<HTMLInputElement>(null);
  const ref = inputRef ?? internalRef;
  return (
    <div className={cn("fs-search", className)}>
      <label className="fs-search-field">
        <Search aria-hidden="true" />
        <input
          {...props}
          className="fs-search-input"
          onChange={(event) => onValueChange(event.target.value)}
          ref={ref}
          type="search"
          value={value}
        />
      </label>
      {value && !props.disabled && !props.readOnly ? (
        <SearchClearButton onClear={() => {
          onValueChange("");
          ref.current?.focus();
        }} />
      ) : null}
    </div>
  );
}
