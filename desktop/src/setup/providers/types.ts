import type { QueryClient } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import type { ReactNode } from "react";

import type { ProjectServices, ServiceKind, ServiceSelection } from "../../api/client";

type Translate = TFunction | ((key: string, options?: Record<string, unknown>) => string);

/** Why a form cannot be saved, by field. An empty message still blocks the
 * save, for a state the panel already shows (a registration in flight). */
export type FieldErrors = Record<string, string>;

/** How the integrations section edits the items of one kind a provider serves. */
export type ServiceUi = {
  /** The items a kind starts with when this provider is chosen for it. */
  initial(services: ProjectServices): Record<string, unknown>;
  errors(value: ServiceSelection, t: Translate): FieldErrors;
  Fields(props: {
    value: ServiceSelection;
    onChange(value: ServiceSelection): void;
    errors: FieldErrors;
    /** Every kind the project names, as the form holds them. */
    services: ProjectServices;
  }): ReactNode;
};

/** What a member's account panel knows of the member around it. */
export type MemberContext = {
  human: boolean;
  /** The member's ID as the form holds it. */
  personId: string;
  /** The saved member being edited, or "" for one being added. */
  savedPersonId: string;
  /** The edit session: a change means the form now describes another member. */
  memberKey: string;
  gitEmail: string;
  setGitEmail(value: string): void;
  /** Member-relative secret name -> the key its value is stored under. */
  secretEnvKeys: Record<string, string>;
  chatChannels: string[];
  /** The editor of the member's chat channels, for the chat's panel to place. */
  chatChannelsField: ReactNode;
  services: ProjectServices;
};

/** A member's account with a provider, as the member editor edits it. */
export type MemberAccountUi<Form> = {
  /** The form of a member being added. */
  initial(): Form;
  /** The form of a stored account; ``current`` is the form it replaces. */
  fromView(view: Record<string, unknown> | undefined, current: Form, human: boolean): Form;
  /** The account as a save sends it. */
  request(form: Form, human: boolean): Record<string, unknown>;
  errors(form: Form, context: MemberContext, t: Translate): FieldErrors;
  Panel(props: {
    form: Form;
    onChange(update: (form: Form) => Form): void;
    context: MemberContext;
    errors: FieldErrors;
  }): ReactNode;
  /** Whether the form was set up for the member its ID names, which a member
   * being added then keeps. */
  holdsPersonId?(form: Form): boolean;
  /** After the member was saved; ``failure`` when the save failed. */
  saved?(
    form: Form,
    outcome: {
      failure?: unknown;
      context: MemberContext;
      queryClient: QueryClient;
      t: Translate;
    },
  ): Form;
};

/** What the setup screens know of a provider beyond what the backend says. */
export type ProviderUi = {
  services: Partial<Record<ServiceKind, ServiceUi>>;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- each provider has its own form
  member?: MemberAccountUi<any>;
  /** Whether a member's chat channel is named in a way the chat accepts. */
  chatChannel?(reference: string): boolean;
};
