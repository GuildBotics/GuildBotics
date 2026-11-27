export function formatBinarySize(bytes: number, locale = "en"): string {
  if (bytes >= 2 ** 30) {
    return `${(bytes / 2 ** 30).toLocaleString(locale, {
      minimumFractionDigits: 1,
      maximumFractionDigits: 1,
    })} GiB`;
  }
  if (bytes >= 2 ** 20) return `${Math.round(bytes / 2 ** 20).toLocaleString(locale)} MiB`;
  if (bytes >= 2 ** 10) {
    return `${(bytes / 2 ** 10).toLocaleString(locale, { maximumFractionDigits: 1 })} KiB`;
  }
  return `${bytes.toLocaleString(locale)} B`;
}
