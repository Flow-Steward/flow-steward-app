import { DsBadge, type DsBadgeTone } from "@/components/shared/design-system";
import { localizedStatusLabel } from "@/components/shared/localized-status-label";
import { useI18n } from "@/lib/i18n/useI18n";

export function LocalizedStatusBadge({ value, tone }: Readonly<{ value: string; tone: DsBadgeTone }>) {
  const { t } = useI18n();
  return <DsBadge tone={tone}>{localizedStatusLabel(value, t)}</DsBadge>;
}
