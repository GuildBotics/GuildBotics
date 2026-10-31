import { providerOperation } from "../../../api/client";

export type LaneMap = {
  ready: string;
  working: string;
  done: string;
};

export const DEFAULT_LANE_MAP: LaneMap = { ready: "Todo", working: "In Progress", done: "Done" };

/** The board the form names, which may not be saved yet. */
export type BoardTarget = { url: string };

export type StatusOptions = {
  available: boolean;
  statuses: string[];
};

export type AgentFieldOption = {
  name: string;
  description: string;
};

export type AgentFieldState = {
  available: boolean;
  exists: boolean;
  options: AgentFieldOption[];
  missing: AgentFieldOption[];
};

export type AccountType = "" | "human" | "machine_user" | "github_apps";

/** A member's stored GitHub account, as the backend shows it. */
export type AccountView = {
  account_type: AccountType;
  username: string;
  installation_id: number | null;
  app_id: number | null;
  has_private_key: boolean;
  has_access_token: boolean;
};

export type UserReference = {
  account_type: Exclude<AccountType, "">;
  identity: string;
};

export type ResolvedUser = {
  person_id: string;
  username: string;
  user_id: number;
  git_email: string;
};

export type AppRegistrationStart = {
  app_name: string;
  person_id: string;
  organization?: string;
};

export type AppRegistrationStatus = {
  state: string;
  status: "pending" | "converted" | "installed";
  app_name: string;
  person_id: string;
  start_url: string;
  slug: string;
  app_id: number | null;
  html_url: string;
  username: string;
  git_email: string;
  installation_id: number | null;
  installation_page_url: string;
  installation_check_error: string;
};

export function getStatusOptions(target: BoardTarget): Promise<StatusOptions> {
  return providerOperation("github", "status-options", target);
}

export function getAgentFieldState(target: BoardTarget): Promise<AgentFieldState> {
  return providerOperation("github", "agent-field", target);
}

export function ensureAgentField(target: BoardTarget): Promise<AgentFieldState> {
  return providerOperation("github", "agent-field/ensure", target);
}

export function resolveUser(reference: UserReference): Promise<ResolvedUser> {
  return providerOperation("github", "resolve-user", reference);
}

export function startAppRegistration(body: AppRegistrationStart): Promise<AppRegistrationStatus> {
  return providerOperation("github", "app-registrations", body);
}

export function getAppRegistration(state: string): Promise<AppRegistrationStatus> {
  return providerOperation("github", "app-registration", { state });
}
