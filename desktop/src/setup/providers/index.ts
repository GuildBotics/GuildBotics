import { useQuery } from "@tanstack/react-query";
import type { TFunction } from "i18next";

import {
  SERVICE_KINDS,
  getProviders,
  type ProjectServices,
  type ProviderOption,
  type ServiceKind,
} from "../../api/client";
import { branvilAccount } from "./branvil/BranvilAccount";
import { branvilCodeHosting, branvilTickets } from "./branvil/BranvilServices";
import { githubAccount } from "./github/GitHubAccount";
import { githubBoard, githubCodeHosting } from "./github/GitHubBoard";
import { isSlackChannel, slackAccount } from "./slack/SlackAccount";
import type { FieldErrors, ProviderUi } from "./types";

/** What the setup screens draw for each provider the backend offers, by its
 * name. A provider without an entry is offered with no items of its own. */
export const PROVIDER_UIS: Record<string, ProviderUi> = {
  branvil: {
    services: { code_hosting_service: branvilCodeHosting, ticket_service: branvilTickets },
    member: branvilAccount,
  },
  github: {
    services: { code_hosting_service: githubCodeHosting, ticket_service: githubBoard },
    member: githubAccount,
  },
  slack: {
    services: {},
    member: slackAccount,
    chatChannel: isSlackChannel,
  },
};

/** The providers the backend offers. */
export function useServiceProviders(): ProviderOption[] {
  return (
    useQuery({ queryKey: ["providers"], queryFn: getProviders, staleTime: Infinity }).data ?? []
  );
}

/** Why each chosen kind's items cannot be saved. */
export function serviceErrors(
  services: ProjectServices,
  t: TFunction | ((key: string) => string),
): Partial<Record<ServiceKind, FieldErrors>> {
  return Object.fromEntries(
    SERVICE_KINDS.flatMap((kind) => {
      const value = services[kind];
      const ui = value ? PROVIDER_UIS[value.name]?.services[kind] : undefined;
      return value && ui ? [[kind, ui.errors(value, t)]] : [];
    }),
  );
}

export function servicesReady(
  services: ProjectServices,
  t: TFunction | ((key: string) => string),
): boolean {
  return Object.values(serviceErrors(services, t)).every(
    (errors) => Object.keys(errors ?? {}).length === 0,
  );
}
