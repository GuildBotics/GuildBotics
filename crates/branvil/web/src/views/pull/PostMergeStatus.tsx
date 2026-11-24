import type { PostMergeStatus } from "../../api/types";
import { StateBadge } from "../../components/common";
import { useT } from "../../i18n";
import { CI_TONE } from "../board/ordering";

export function PostMergeStatusBadge({ status }: { status: PostMergeStatus }) {
  const t = useT();
  return (
    <StateBadge tone={CI_TONE[status]}>
      {t("ci.postMergeStatus", { status: t(`ci.${status}`) })}
    </StateBadge>
  );
}
