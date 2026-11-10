import { ExternalLink } from "lucide-react";
import { useTranslation } from "react-i18next";

/** The integrated server shares the browser session across both screens. */
export function BranvilNavItem() {
  const { t } = useTranslation();
  return (
    <a className="nav-item" href="/">
      <ExternalLink size={18} aria-hidden="true" />
      {t("setup.branvil.nav.label")}
    </a>
  );
}
