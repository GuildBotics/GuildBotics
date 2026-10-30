import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import "../../../i18n";
import { TestMantineProvider } from "../../../test/TestMantineProvider";
import type { MemberContext } from "../types";
import { githubAccount, type GitHubForm } from "./GitHubAccount";

const t = i18n.getFixedT("en");
const PEM = "-----BEGIN RSA PRIVATE KEY-----\nkey\n-----END RSA PRIVATE KEY-----\n";

const context: MemberContext = {
  human: false,
  personId: "my-bot",
  savedPersonId: "my-bot",
  memberKey: "edit:my-bot",
  gitEmail: "my-bot@example.com",
  setGitEmail: () => undefined,
  secretEnvKeys: {},
  chatChannels: [],
  chatChannelsField: null,
  services: {},
};

/** Holds the form the way the member editor does and reports each change. */
function Harness({ onForm }: { onForm: (form: GitHubForm) => void }) {
  const [form, setForm] = useState<GitHubForm>({
    ...githubAccount.initial(),
    accountType: "github_apps",
    setupMode: "existing",
  });
  return (
    <githubAccount.Panel
      form={form}
      onChange={(update) =>
        setForm((current) => {
          const next = update(current);
          onForm(next);
          return next;
        })
      }
      context={context}
      errors={{}}
    />
  );
}

describe("GitHub App private key", () => {
  afterEach(() => {
    delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  });

  it.each([
    ["the browser", false],
    ["the Desktop window", true],
  ])("sends the chosen file's PEM text in %s", async (_where, desktop) => {
    if (desktop) {
      (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    }
    const onForm = vi.fn();
    render(
      <TestMantineProvider>
        <Harness onForm={onForm} />
      </TestMantineProvider>,
    );

    const input = document.querySelector<HTMLInputElement>('input[type="file"]');
    expect(screen.getByText(t("setup.members.privateKey"))).toBeInTheDocument();
    await userEvent.upload(input!, new File([PEM], "my-bot.private-key.pem"));

    await waitFor(() => expect(onForm).toHaveBeenCalled());
    const form = onForm.mock.lastCall![0] as GitHubForm;
    expect(githubAccount.request(form, false)).toMatchObject({ private_key: PEM });
    expect(githubAccount.request(form, false)).not.toHaveProperty("private_key_path");
    expect(screen.getByText("my-bot.private-key.pem")).toBeInTheDocument();
  });
});
