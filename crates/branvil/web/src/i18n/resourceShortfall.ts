import type { ResourceShortfall } from "../api/types";
import type { Translate } from ".";

export function resourceShortfallText(t: Translate, name: string, wait: ResourceShortfall): string {
  const { cpu_shortfall: cpus, memory_shortfall_mib: memory, commit_shortfall_mib: commit } = wait;
  const messages = [];
  if (cpus && memory) {
    messages.push(t("ci.settings.resourceWait", { name, cpus, memory }));
  } else if (cpus) {
    messages.push(t("ci.settings.resourceWaitCpu", { name, cpus }));
  } else if (memory) {
    messages.push(t("ci.settings.resourceWaitMemory", { name, memory }));
  }
  if (commit) messages.push(t("ci.settings.resourceWaitCommit", { name, commit }));
  return messages.join(" — ");
}
