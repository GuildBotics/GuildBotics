import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import type { AgentEnvironmentDeclaration } from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { AgentEnvironmentDeclarationCard } from "./AgentEnvironmentDeclarationCard";

const t = i18n.getFixedT("en");

const packaged: AgentEnvironmentDeclaration = {
  packages: { apt: [], npm: [], uv: [] },
  dns: { nameservers: "host" },
};

function Harness({
  initial = packaged,
  onChange,
  onValidityChange,
}: {
  initial?: AgentEnvironmentDeclaration;
  onChange?: (value: AgentEnvironmentDeclaration) => void;
  onValidityChange?: (valid: boolean) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <MantineProvider env="test">
      <AgentEnvironmentDeclarationCard
        value={value}
        onChange={(next) => {
          setValue(next);
          onChange?.(next);
        }}
        onValidityChange={onValidityChange}
      />
    </MantineProvider>
  );
}

describe("AgentEnvironmentDeclarationCard", () => {
  it("adds a pinned package to its manager's list", async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);

    const npm = screen.getByRole("combobox", {
      name: t("setup.intelligence.environment.declaration.npm"),
    });
    await userEvent.type(npm, "typescript@5.6.3{enter}");

    expect(onChange).toHaveBeenLastCalledWith({
      packages: { apt: [], npm: ["typescript@5.6.3"], uv: [] },
      dns: { nameservers: "host" },
    });
  });

  it("refuses an entry that is not one package argument", async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);

    const apt = screen.getByRole("combobox", {
      name: t("setup.intelligence.environment.declaration.apt"),
    });
    await userEvent.type(apt, "--force-yes{enter}");

    expect(
      screen.getByText(t("setup.intelligence.environment.declaration.invalidPackage")),
    ).toBeInTheDocument();
    expect(onChange).toHaveBeenLastCalledWith(packaged);
  });

  it("switches the resolvers between this device's and a fixed IPv4 list", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const onValidityChange = vi.fn();
    render(<Harness onChange={onChange} onValidityChange={onValidityChange} />);

    await user.click(
      screen.getByRole("combobox", {
        name: t("setup.intelligence.environment.declaration.nameservers"),
      }),
    );
    await user.click(
      await screen.findByRole("option", {
        name: t("setup.intelligence.environment.declaration.nameserversList"),
      }),
    );

    // An empty list is not a declaration the backend accepts, so the save waits.
    expect(onChange).toHaveBeenLastCalledWith({ ...packaged, dns: { nameservers: [] } });
    expect(
      screen.getByText(t("setup.intelligence.environment.declaration.emptyNameservers")),
    ).toBeInTheDocument();
    expect(onValidityChange).toHaveBeenLastCalledWith(false);

    const list = screen.getByRole("combobox", {
      name: t("setup.intelligence.environment.declaration.nameserversList"),
    });
    await user.type(list, "not-an-ip{enter}");
    expect(
      screen.getByText(t("setup.intelligence.environment.declaration.invalidNameserver")),
    ).toBeInTheDocument();
    await user.type(list, "10.0.0.53{enter}");

    expect(onChange).toHaveBeenLastCalledWith({ ...packaged, dns: { nameservers: ["10.0.0.53"] } });
    expect(onValidityChange).toHaveBeenLastCalledWith(true);
  });

  it("shows a fixed list as it is declared", () => {
    render(
      <Harness
        initial={{
          packages: { apt: ["ripgrep=14.1.0-1"], npm: [], uv: ["ruff==0.6.9"] },
          dns: { nameservers: ["1.1.1.1", "1.0.0.1"] },
        }}
      />,
    );

    expect(screen.getByText("ripgrep=14.1.0-1")).toBeInTheDocument();
    expect(screen.getByText("ruff==0.6.9")).toBeInTheDocument();
    expect(screen.getByText("1.1.1.1")).toBeInTheDocument();
    expect(screen.getByText("1.0.0.1")).toBeInTheDocument();
  });
});
