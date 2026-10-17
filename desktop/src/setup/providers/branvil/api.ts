import { connected, providerOperation, type ProjectServices } from "../../../api/client";

/** Where Branvil is reached unless the project says otherwise: the same on
 * every machine, through Branvil's tunnel on those other than the Hub. */
export const BRANVIL_BASE_URL = "http://127.0.0.1:7878";

export const ORIGIN = /^https?:\/\/[^/\s?#@]+\/?$/;

/** GuildBotics' connection to Branvil, as the backend reports it. */
export type Connection = {
  status: "connected" | "pending" | "disconnected";
  /** The name of Branvil's owner, whose rights the connection has. */
  owner: string;
  /** Why it is not connected. */
  message: string;
};

export type ConsentStarted = { state: string; url: string };

/** The Branvil the project's services name, "" when none is it. */
export function branvilUrl(services: ProjectServices): string {
  const service = Object.values(services).find((item) => item?.name === "branvil");
  return service ? String(service.base_url ?? "").trim() || BRANVIL_BASE_URL : "";
}

/** A query key of what a Branvil answers. */
export const branvilKey = (what: string, baseUrl: string) => ["branvil", what, baseUrl];

function operation<T>(name: string, baseUrl: string, body: object = {}): Promise<T> {
  return providerOperation("branvil", name, { base_url: baseUrl, ...body });
}

export function startConnection(baseUrl: string): Promise<ConsentStarted> {
  return operation("connect", baseUrl, { callback_base_url: connected().base });
}

export function getConnection(baseUrl: string, state = ""): Promise<Connection> {
  return operation("connection", baseUrl, { state });
}

export function listWorkspaces(baseUrl: string): Promise<{ workspaces: string[] }> {
  return operation("workspaces", baseUrl);
}

export function createWorkspace(baseUrl: string, name: string): Promise<{ workspaces: string[] }> {
  return operation("workspaces/create", baseUrl, { name });
}

export function listAgents(baseUrl: string): Promise<{ agents: string[] }> {
  return operation("agents", baseUrl);
}

/** A single-use link into Branvil's GUI. */
export function enterBranvil(baseUrl: string): Promise<{ url: string }> {
  return operation("entry", baseUrl);
}
