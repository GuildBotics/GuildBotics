/** Open a page outside GuildBotics in a new tab, with no reference back to GuildBotics. */
export async function openExternal(url: string): Promise<void> {
  window.open(url, "_blank", "noopener,noreferrer");
}
