import { providerOperation } from "../../../api/client";

/** Where Branvil is reached unless connected elsewhere: the same on every
 * machine, through Branvil's tunnel on those other than the Hub. */
export const BRANVIL_BASE_URL = "http://127.0.0.1:7878";

export const ORIGIN = /^https?:\/\/[^/\s?#@]+\/?$/;

/** GuildBotics' connection to Branvil, as the backend reports it. It is the
 * identities' -- one for every workspace -- not a workspace's. */
export type Connection = {
  status: "connected" | "pending" | "disconnected";
  /** Where the connection reaches Branvil; empty before one is made. */
  base_url: string;
  /** The name of Branvil's owner, whose rights the connection has. */
  owner: string;
  /** Why it is not connected. */
  message: string;
  /** What the owner adds to their git configuration, while connected, for
   * their own git to authenticate to Branvil. */
  git_config: string;
};

export type ConsentStarted = { state: string; url: string };

/** A query key of what the connected Branvil answers. */
export const branvilKey = (what: string) => ["branvil", what];

function operation<T>(name: string, body: object = {}): Promise<T> {
  return providerOperation("branvil", name, body);
}

/** Start connecting to the Branvil at ``baseUrl``, on its owner's consent. */
export function startConnection(baseUrl: string): Promise<ConsentStarted> {
  return operation("connect", { base_url: baseUrl });
}

export function getConnection(state = ""): Promise<Connection> {
  return operation("connection", { state });
}

export function listWorkspaces(): Promise<{ workspaces: string[] }> {
  return operation("workspaces");
}

export function createWorkspace(name: string): Promise<{ workspaces: string[] }> {
  return operation("workspaces/create", { name });
}

export function listAgents(): Promise<{ agents: string[] }> {
  return operation("agents");
}

/** A single-use link into Branvil's GUI. */
export function enterBranvil(): Promise<{ url: string }> {
  return operation("entry");
}
