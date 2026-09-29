import { useTranslation } from "react-i18next";

import type { CliAgentLastTurn } from "../api/client";

export function CliAgentLastTurnDetails({ turn }: { turn: CliAgentLastTurn | undefined }) {
  const { t, i18n } = useTranslation();
  if (!turn) {
    return <span className="cli-agent-last-turn">{t("setup.intelligence.lastTurn.notRun")}</span>;
  }
  return (
    <span className="cli-agent-last-turn">
      <span>
        {turn.model || turn.model_specified
          ? `${turn.model || t("setup.intelligence.lastTurn.unknown")} (${t(`setup.intelligence.lastTurn.${turn.model_specified ? "specified" : "default"}`)})`
          : t("setup.intelligence.lastTurn.unavailable")}
      </span>
      <span>
        {t("setup.intelligence.lastTurn.time", {
          time: new Date(turn.timestamp).toLocaleString(i18n.language),
        })}
      </span>
    </span>
  );
}
