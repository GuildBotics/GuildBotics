import { useEffect } from "react";
import { useWorkspaces } from "./query/hooks";
import { useStored } from "../ui/storage";

export function useCurrentWorkspace(
  fromRoute: string | undefined,
  hubWorkspace: string | undefined,
  available: boolean,
) {
  const query = useWorkspaces(available);
  const [last, remember] = useStored("branvil.workspace");
  const names = query.data?.items.map((workspace) => workspace.name);
  const name =
    fromRoute ?? names?.find((name) => name === last) ?? hubWorkspace ?? names?.[0] ?? null;
  useEffect(() => {
    if (fromRoute) remember(fromRoute);
  }, [fromRoute, remember]);
  return { name, names, query, rememberedName: last };
}

export type CurrentWorkspace = ReturnType<typeof useCurrentWorkspace>;
