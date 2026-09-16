import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { type NetworkPolicy } from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { NetworkPolicyField } from "./NetworkPolicyField";
import { TestMantineProvider } from "../test/TestMantineProvider";

const t = i18n.getFixedT("en");
const CLOSED_NETWORK_POLICY: NetworkPolicy = {
  mode: "deny",
  allowed_domains: [],
  allow_local_network: false,
};

function Harness({
  initial = CLOSED_NETWORK_POLICY,
  onChange,
  onValidityChange,
}: {
  initial?: NetworkPolicy;
  onChange?: (value: NetworkPolicy) => void;
  onValidityChange?: (valid: boolean) => void;
}) {
  const [value, setValue] = useState<NetworkPolicy>(initial);
  return (
    <TestMantineProvider>
      <NetworkPolicyField
        value={value}
        onChange={(next) => {
          setValue(next);
          onChange?.(next);
        }}
        onValidityChange={onValidityChange}
      />
    </TestMantineProvider>
  );
}

describe("NetworkPolicyField", () => {
  it("asks for domains only under allowlist and drops them elsewhere", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);

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
    expect(onChange).toHaveBeenLastCalledWith(CLOSED_NETWORK_POLICY);
  });

  it("blocks an empty or malformed allowlist before save", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const onValidityChange = vi.fn();
    render(
      <Harness
        initial={{ ...CLOSED_NETWORK_POLICY, mode: "allowlist" }}
        onChange={onChange}
        onValidityChange={onValidityChange}
      />,
    );

    expect(screen.getByText(t("setup.intelligence.network.emptyAllowlist"))).toBeInTheDocument();
    expect(onValidityChange).toHaveBeenLastCalledWith(false);
    await user.type(
      screen.getByRole("combobox", { name: t("setup.intelligence.network.allowedDomains") }),
      "https://example.com/path{enter}",
    );
    expect(screen.getByText(t("setup.intelligence.network.invalidDomain"))).toBeInTheDocument();
    expect(onChange).toHaveBeenLastCalledWith({
      ...CLOSED_NETWORK_POLICY,
      mode: "allowlist",
    });
  });

  it("warns when unrestricted access is selected", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole("combobox", { name: t("setup.intelligence.network.mode") }));
    await user.click(
      await screen.findByRole("option", {
        name: t("setup.intelligence.network.modes.unrestricted"),
      }),
    );

    expect(screen.getByText(t("setup.intelligence.network.unrestrictedWarning"))).toBeVisible();
  });
});
