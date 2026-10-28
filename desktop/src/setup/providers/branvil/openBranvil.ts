import { openExternal } from "../../../openExternal";
import { enterBranvil } from "./api";

/** Request a fresh single-use entry link and open it in the system browser. */
export async function openBranvil(): Promise<void> {
  await openExternal((await enterBranvil()).url);
}
