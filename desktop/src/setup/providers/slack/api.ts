import { providerOperation } from "../../../api/client";

/** A member's stored Slack account, as the backend shows it. */
export type AccountView = {
  user_id: string;
  has_bot_token: boolean;
  has_app_token: boolean;
};

export type SlackAppRegistrationStatus = {
  app_name: string;
  registration_url: string;
  app_directory_url: string;
};

export type SlackTokenVerifyRequest = {
  bot_token: string;
  app_token: string;
  person_id?: string;
  channels?: string[];
};

export type SlackChannelVerification = {
  channel: string;
  ok: boolean;
  error: string;
};

// Which token was actually checked: the one typed in, the one already saved
// for the member (an empty field keeps it), or neither.
export type SlackTokenSource = "input" | "stored" | "none";

export type SlackTokenVerifyResponse = {
  bot_ok: boolean;
  bot_user_id: string;
  bot_display_name: string;
  workspace: string;
  bot_error: string;
  bot_source: SlackTokenSource;
  scopes_ok: boolean;
  scope_error: string;
  scope_needed: string;
  app_token_ok: boolean;
  app_token_error: string;
  app_token_source: SlackTokenSource;
  channels: SlackChannelVerification[];
};

export function startSlackAppRegistration(body: {
  app_name: string;
}): Promise<SlackAppRegistrationStatus> {
  return providerOperation("slack", "app-registrations", body);
}

export function verifySlackTokens(
  body: SlackTokenVerifyRequest,
): Promise<SlackTokenVerifyResponse> {
  return providerOperation("slack", "verify-tokens", body);
}
