import { useQuery } from "@tanstack/react-query";
import { getProviders, type ProviderOption } from "../../api/client";
import { isSlackChannel, slackAccount } from "./slack/SlackAccount";
import type { ProviderUi } from "./types";

/** What the setup screens draw for each provider the backend offers, by its
 * name. A provider without an entry is offered with no items of its own. */
export const PROVIDER_UIS: Record<string, ProviderUi> = {
  slack: {
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
