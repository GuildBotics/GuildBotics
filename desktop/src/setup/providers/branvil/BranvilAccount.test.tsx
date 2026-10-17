import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import i18n from "../../../i18n";
import "../../../i18n";
import type { ServiceSelection } from "../../../api/client";
import { TestMantineProvider } from "../../../test/TestMantineProvider";
import { MASKED_SECRET_PLACEHOLDER } from "../../secretInput";
import type { MemberContext } from "../types";
import { branvilAccount, type BranvilForm } from "./BranvilAccount";
import { branvilTickets } from "./BranvilServices";

const t = i18n.getFixedT("en");

const memberContext = (human: boolean): MemberContext => ({
  human,
  personId: "aiko",
  savedPersonId: "aiko",
  memberKey: "aiko",
  gitEmail: "",
  setGitEmail: () => undefined,
  secretEnvKeys: {},
  chatChannels: [],
  chatChannelsField: null,
  services: {},
});

function renderWithProviders(node: React.ReactNode) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <TestMantineProvider>{node}</TestMantineProvider>
    </QueryClientProvider>,
  );
}

function Account({ start, human }: { start: BranvilForm; human: boolean }) {
  const [form, setForm] = useState(start);
  return (
    <>
      <branvilAccount.Panel
        form={form}
        onChange={(update) => setForm(update)}
        context={memberContext(human)}
        errors={{}}
      />
      <output>{JSON.stringify(branvilAccount.request(form, human))}</output>
    </>
  );
}

describe("BranvilAccount panel", () => {
  it("shows a stored token as masked and sends a new one typed over it", async () => {
    const start = branvilAccount.fromView(
      { username: "aiko", has_token: true },
      branvilAccount.initial(),
      false,
    );
    renderWithProviders(<Account start={start} human={false} />);

    const token = screen.getByLabelText(t("setup.branvil.token"));
    expect(token).toHaveAttribute("placeholder", MASKED_SECRET_PLACEHOLDER);
    expect(token).toHaveAttribute("aria-required", "false");
    await userEvent.type(token, "bvt_new");

    expect(screen.getByRole("status")).toHaveTextContent('{"username":"aiko","token":"bvt_new"}');
  });

  it("asks a human member for the name only", () => {
    renderWithProviders(<Account start={branvilAccount.initial()} human />);

    expect(screen.getByLabelText(t("setup.branvil.username"))).toBeInTheDocument();
    expect(screen.queryByLabelText(t("setup.branvil.token"))).not.toBeInTheDocument();
  });
});

function Tickets() {
  const [value, setValue] = useState<ServiceSelection>({
    name: "branvil",
    ...branvilTickets.initial({}),
  });
  return (
    <>
      <branvilTickets.Fields
        value={value}
        onChange={setValue}
        errors={branvilTickets.errors(value, t)}
      />
      <output>{JSON.stringify(value)}</output>
    </>
  );
}

describe("Branvil ticket service fields", () => {
  it("edits the base URL, the namespace, and the board", async () => {
    renderWithProviders(<Tickets />);

    expect(screen.getByText(t("setup.branvil.ownerRequired"))).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(t("setup.branvil.owner")), "acme");
    await userEvent.type(screen.getByLabelText(t("setup.branvil.board")), "acme/board");

    expect(screen.queryByText(t("setup.branvil.ownerRequired"))).not.toBeInTheDocument();
    expect(JSON.parse(screen.getByRole("status").textContent ?? "")).toEqual({
      name: "branvil",
      base_url: "http://127.0.0.1:7878",
      owner: "acme",
      board: "acme/board",
    });
  });
});
