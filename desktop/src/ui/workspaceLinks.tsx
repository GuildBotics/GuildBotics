import type { ComponentProps } from "react";
import { Link, NavLink, useLocation } from "react-router";
import { workspaceFromPath } from "../api/client";
import { workspacePath } from "../repository/router/routes";

type Props = Omit<ComponentProps<typeof Link>, "to"> & { to: string };

export function useWorkspacePath() {
  const workspace = workspaceFromPath(useLocation().pathname);
  return workspace ? workspacePath(workspace) : "";
}

export function WorkspaceLink({ to, ...props }: Props) {
  const prefix = useWorkspacePath();
  return <Link to={to.startsWith("/workspaces/") ? to : prefix + to} {...props} />;
}

export function WorkspaceNavLink({ to, ...props }: Props) {
  const prefix = useWorkspacePath();
  return <NavLink to={to.startsWith("/workspaces/") ? to : prefix + to} {...props} />;
}
