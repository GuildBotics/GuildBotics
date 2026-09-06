import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  CLOSED_NETWORK_POLICY,
  type CliAgentNetworkSupport,
  type NetworkPolicy,
} from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { NetworkPolicyField } from "./NetworkPolicyField";

const t = i18n.getFixedT("en");

const CODEX: CliAgentNetworkSupport = {
  modes: ["allowlist", "deny", "unrestricted"],
  modes_anywhere: ["allowlist", "deny", "unrestricted"],
  local_network_modes: ["allowlist"],
  grant_accesses: ["read", "read_write"],
  contract_applied: true,
};

const GROK_ON_MAC: CliAgentNetworkSupport = {
  ...CODEX,
  modes: ["unrestricted"],
  modes_anywhere: ["deny", "unrestricted"],
  local_network_modes: [],
  contract_applied: false,
};

function Harness({
  initial = null,
  support = CODEX,
  isToolDefault = false,
  onChange,
}: {
  initial?: NetworkPolicy | null;
  support?: CliAgentNetworkSupport;
  isToolDefault?: boolean;
  onChange?: (value: NetworkPolicy | null) => void;
}) {
  const [value, setValue] = useState<NetworkPolicy | null>(initial);
  return (
    <MantineProvider env="test">
      <NetworkPolicyField
        value={value}
        inherited={CLOSED_NETWORK_POLICY}
        tool="codex"
        toolLabel="Codex"
        support={support}
        isToolDefault={isToolDefault}
        onChange={(next) => {
          setValue(next);
          onChange?.(next);
        }}
      />
    </MantineProvider>
  );
}

describe("NetworkPolicyField", () => {
  it("shows the inherited block read-only until the slot customizes it", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);

    expect(screen.getByText(t("setup.intelligence.network.inherited"))).toBeInTheDocument();
    const mode = screen.getByRole("combobox", { name: t("setup.intelligence.network.mode") });
    expect(mode).toBeDisabled();

    await user.click(
      screen.getByRole("button", { name: t("setup.intelligence.network.customize") }),
    );

    // The slot now states the whole block, starting from what it inherited.
    expect(onChange).toHaveBeenLastCalledWith(CLOSED_NETWORK_POLICY);
    expect(
      screen.getByRole("button", { name: t("setup.intelligence.network.useDefault") }),
    ).toBeInTheDocument();
  });

  it("asks for domains only under allowlist and drops them elsewhere", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness initial={structuredClone(CLOSED_NETWORK_POLICY)} onChange={onChange} />);

    const mode = screen.getByRole("combobox", { name: t("setup.intelligence.network.mode") });
    await user.click(mode);
    await user.click(
      await screen.findByRole("option", { name: t("setup.intelligence.network.modes.allowlist") }),
    );

    const domains = screen.getByRole("combobox", {
      name: t("setup.intelligence.network.allowedDomains"),
    });
    await user.type(domains, "registry.npmjs.org{enter}");
    expect(onChange).toHaveBeenLastCalledWith({
      mode: "allowlist",
      allowed_domains: ["registry.npmjs.org"],
      allow_local_network: false,
    });

    await user.click(mode);
    await user.click(
      await screen.findByRole("option", { name: t("setup.intelligence.network.modes.deny") }),
    );
    expect(onChange).toHaveBeenLastCalledWith({
      mode: "deny",
      allowed_domains: [],
      allow_local_network: false,
    });
  });

  it("explains a mode this device cannot enforce and warns when the tool ignores the block", () => {
    render(<Harness initial={structuredClone(CLOSED_NETWORK_POLICY)} support={GROK_ON_MAC} />);

    // Shown as the select's description and, since it is the selected mode,
    // as its error too.
    expect(
      screen.getAllByText(t("setup.intelligence.network.unsupportedHere", { tool: "Codex" })),
    ).toHaveLength(2);
    expect(
      screen.getByText(t("setup.intelligence.network.contractPending", { tool: "Codex" })),
    ).toBeInTheDocument();
    // Local network cannot be opened separately under deny for this tool.
    expect(screen.getByRole("switch")).toBeDisabled();
  });

  it("edits the tool's own default directly, with only a reset to the packaged values", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness isToolDefault onChange={onChange} />);

    expect(screen.queryByText(t("setup.intelligence.network.inherited"))).not.toBeInTheDocument();
    const mode = screen.getByRole("combobox", { name: t("setup.intelligence.network.mode") });
    expect(mode).toBeEnabled();
    const reset = screen.getByRole("button", {
      name: t("setup.intelligence.network.resetToPackaged"),
    });
    expect(reset).toBeDisabled();

    await user.click(mode);
    await user.click(
      await screen.findByRole("option", {
        name: t("setup.intelligence.network.modes.unrestricted"),
      }),
    );
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ mode: "unrestricted" }));

    await user.click(reset);
    expect(onChange).toHaveBeenLastCalledWith(CLOSED_NETWORK_POLICY);
  });
});
