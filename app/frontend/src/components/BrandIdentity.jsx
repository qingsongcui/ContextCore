import { t } from "../lib/i18n.jsx";
export function BrandIdentity() {
  return <><span className="brand-symbol" aria-hidden="true"><img src="/senrelay-logo-original-v1.png" alt="" /></span><span className="brand-wordmark">Senrelay<span className="brand-chinese">{t("· 续境")}</span></span></>;
}
