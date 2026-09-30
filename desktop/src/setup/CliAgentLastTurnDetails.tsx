import { useTranslation } from "react-i18next";

import type { CliAgentLastTurn } from "../api/client";

export function CliAgentLastTurnDetails({ turn }: { turn: CliAgentLastTurn | undefined }) {
  const { t, i18n } = useTranslation();
  if (!turn) {
    return <span className="cli-agent-last-turn">{t("setup.intelligence.lastTurn.noRecord")}</span>;
  }
  const source = t(`setup.intelligence.lastTurn.${turn.model_specified ? "specified" : "default"}`);
  const summary =
    turn.model || turn.model_specified
      ? t("setup.intelligence.lastTurn.modelWithSource", {
          model: turn.model || t("setup.intelligence.lastTurn.modelNotReported"),
          source,
        })
      : t("setup.intelligence.lastTurn.defaultModelNotReported");
  return (
    <span className="cli-agent-last-turn">
      <span>{summary}</span>
      <span>
        {t("setup.intelligence.lastTurn.time", {
          time: new Date(turn.timestamp).toLocaleString(i18n.language),
        })}
      </span>
    </span>
  );
}
