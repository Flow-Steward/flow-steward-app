import { Eye, EyeOff } from "lucide-react";

type PasswordVisibilityToggleProps = {
  hideLabel: string;
  onToggle: () => void;
  showLabel: string;
  visible: boolean;
};

export function PasswordVisibilityToggle({
  hideLabel,
  onToggle,
  showLabel,
  visible,
}: Readonly<PasswordVisibilityToggleProps>) {
  return (
    <button
      aria-label={visible ? hideLabel : showLabel}
      aria-pressed={visible}
      className="fs-public-auth__reveal-button"
      data-state={visible ? "visible" : "masked"}
      onClick={onToggle}
      type="button"
    >
      <span aria-hidden="true" className="fs-public-auth__reveal-glyph">
        <Eye data-icon data-visible={visible ? "true" : "false"} size={16} strokeWidth={2} />
        <EyeOff data-icon data-visible={visible ? "false" : "true"} size={16} strokeWidth={2} />
      </span>
    </button>
  );
}
