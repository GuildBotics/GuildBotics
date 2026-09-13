import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { getAgentEnvironmentImages, type AgentEnvironmentDeclaration } from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { AgentEnvironmentDeclarationCard } from "./AgentEnvironmentDeclarationCard";

vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  getAgentEnvironmentImages: vi.fn(),
}));

const t = i18n.getFixedT("en");
const DIGEST = "sha256:" + "c".repeat(64);
const OTHER = "sha256:" + "d".repeat(64);

const packaged: AgentEnvironmentDeclaration = {
  packages: { apt: [], npm: [], uv: [] },
  dns: { nameservers: ["1.1.1.1", "8.8.8.8"] },
};
const onHost: AgentEnvironmentDeclaration = { ...packaged, dns: { nameservers: "host" } };

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
  const [client] = useState(
    () => new QueryClient({ defaultOptions: { queries: { retry: false } } }),
  );
  return (
    <MantineProvider env="test">
      <QueryClientProvider client={client}>
        <AgentEnvironmentDeclarationCard
          value={value}
          defaultImage="node:22.23.2-bookworm"
          onChange={(next) => {
            setValue(next);
            onChange?.(next);
          }}
          onValidityChange={onValidityChange}
        />
      </QueryClientProvider>
    </MantineProvider>
  );
}

describe("AgentEnvironmentDeclarationCard", () => {
  beforeEach(() => {
    vi.mocked(getAgentEnvironmentImages).mockResolvedValue({
      architecture: "arm64",
      images: [
        {
          reference: "local/other:2",
          digest: "sha256:" + "a".repeat(64),
          size_bytes: 1,
          declared: false,
        },
        { reference: "local/agent:1", digest: DIGEST, size_bytes: 900, declared: false },
      ],
      problem: "",
    });
  });

  it("names the base image by the digest of the image loaded on this device", async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const select = screen.getByRole("combobox", {
      name: t("setup.intelligence.environment.declaration.image"),
    });
    expect(select).toHaveValue(
      t("setup.intelligence.environment.declaration.imageDefault", {
        reference: "node:22.23.2-bookworm",
      }),
    );

    await userEvent.click(select);
    await userEvent.click(await screen.findByRole("option", { name: /local\/agent:1/ }));

    expect(onChange).toHaveBeenLastCalledWith({
      ...packaged,
      image: { reference: "local/agent:1", digests: { arm64: DIGEST } },
    });
    expect(
      screen.getByText(
        t("setup.intelligence.environment.declaration.imageDeclared", {
          digests: `arm64=${DIGEST.slice(0, 19)}`,
        }),
      ),
    ).toBeInTheDocument();

    await userEvent.click(select);
    await userEvent.click(await screen.findByRole("option", { name: /GuildBotics default/ }));
    expect(onChange).toHaveBeenLastCalledWith({ ...packaged, image: null });
  });

  it("adds this device's architecture to an image another architecture declared", async () => {
    const onChange = vi.fn();
    render(
      <Harness
        initial={{ ...packaged, image: { reference: "local/agent:1", digests: { amd64: OTHER } } }}
        onChange={onChange}
      />,
    );
    const select = screen.getByRole("combobox", {
      name: t("setup.intelligence.environment.declaration.image"),
    });
    // Declared elsewhere, not here: the select shows nothing picked and says why.
    expect(
      await screen.findByText(
        t("setup.intelligence.environment.declaration.imageNotDeclaredHere", {
          reference: "local/agent:1",
          architecture: "arm64",
          declared: "amd64",
        }),
      ),
    ).toBeInTheDocument();

    await userEvent.click(select);
    await userEvent.click(await screen.findByRole("option", { name: /local\/agent:1/ }));

    expect(onChange).toHaveBeenLastCalledWith({
      ...packaged,
      image: { reference: "local/agent:1", digests: { amd64: OTHER, arm64: DIGEST } },
    });
  });

  it("drops other architectures' digests when a different image is picked", async () => {
    const onChange = vi.fn();
    render(
      <Harness
        initial={{ ...packaged, image: { reference: "local/agent:2", digests: { amd64: OTHER } } }}
        onChange={onChange}
      />,
    );
    const select = screen.getByRole("combobox", {
      name: t("setup.intelligence.environment.declaration.image"),
    });
    await userEvent.click(select);
    await userEvent.click(await screen.findByRole("option", { name: /local\/agent:1/ }));

    expect(onChange).toHaveBeenLastCalledWith({
      ...packaged,
      image: { reference: "local/agent:1", digests: { arm64: DIGEST } },
    });
  });

  it("shows a declared image this device lacks as the current, unpickable choice", async () => {
    const onChange = vi.fn();
    render(
      <Harness
        initial={{ ...packaged, image: { reference: "local/agent:2", digests: { arm64: OTHER } } }}
        onChange={onChange}
      />,
    );
    const select = screen.getByRole("combobox", {
      name: t("setup.intelligence.environment.declaration.image"),
    });
    await waitFor(() =>
      expect(select).toHaveValue(
        `local/agent:2 (${OTHER.slice(0, 19)}) — ${t("setup.intelligence.environment.declaration.imageNotHere")}`,
      ),
    );

    await userEvent.click(select);
    const declared = await screen.findByRole("option", { name: /local\/agent:2/ });
    expect(declared).toHaveAttribute("data-combobox-disabled", "true");
    await userEvent.click(await screen.findByRole("option", { name: /local\/agent:1/ }));
    expect(onChange).toHaveBeenLastCalledWith({
      ...packaged,
      image: { reference: "local/agent:1", digests: { arm64: DIGEST } },
    });
  });

  it("keeps the declared reference as the current choice when only an alias holds its digest", async () => {
    // `image load --tag` can name one image twice; the declaration names a
    // reference, so an alias at the same digest does not stand in for it.
    vi.mocked(getAgentEnvironmentImages).mockResolvedValue({
      architecture: "arm64",
      images: [{ reference: "local/alias:1", digest: DIGEST, size_bytes: 900, declared: false }],
      problem: "",
    });
    render(
      <Harness
        initial={{ ...packaged, image: { reference: "local/agent:1", digests: { arm64: DIGEST } } }}
      />,
    );
    const select = screen.getByRole("combobox", {
      name: t("setup.intelligence.environment.declaration.image"),
    });
    await waitFor(() =>
      expect(select).toHaveValue(
        `local/agent:1 (${DIGEST.slice(0, 19)}) — ${t("setup.intelligence.environment.declaration.imageNotHere")}`,
      ),
    );
    await userEvent.click(select);
    expect(await screen.findByRole("option", { name: /local\/agent:1/ })).toHaveAttribute(
      "data-combobox-disabled",
      "true",
    );
    expect(screen.getByRole("option", { name: /local\/alias:1/ })).not.toHaveAttribute(
      "data-combobox-disabled",
      "true",
    );
  });

  it("offers an image loaded again under a new digest beside the declared one", async () => {
    const onChange = vi.fn();
    render(
      <Harness
        initial={{ ...packaged, image: { reference: "local/agent:1", digests: { arm64: OTHER } } }}
        onChange={onChange}
      />,
    );
    const select = screen.getByRole("combobox", {
      name: t("setup.intelligence.environment.declaration.image"),
    });
    await waitFor(() =>
      expect(select).toHaveValue(
        `local/agent:1 (${OTHER.slice(0, 19)}) — ${t("setup.intelligence.environment.declaration.imageNotHere")}`,
      ),
    );
    // The declared digest is not loaded here, the new one is: two choices.
    await userEvent.click(select);
    expect(
      await screen.findByRole("option", { name: new RegExp(OTHER.slice(0, 19)) }),
    ).toHaveAttribute("data-combobox-disabled", "true");
    await userEvent.click(screen.getByRole("option", { name: new RegExp(DIGEST.slice(0, 19)) }));

    expect(onChange).toHaveBeenLastCalledWith({
      ...packaged,
      image: { reference: "local/agent:1", digests: { arm64: DIGEST } },
    });
  });

  it("says why this device's images cannot be offered", async () => {
    vi.mocked(getAgentEnvironmentImages).mockResolvedValue({
      architecture: "arm64",
      images: [],
      problem: "no hypervisor",
    });
    render(<Harness />);

    expect(
      await screen.findByText(
        t("setup.intelligence.environment.declaration.imageUnavailable", {
          problem: "no hypervisor",
        }),
      ),
    ).toBeInTheDocument();
  });

  it("adds a pinned package to its manager's list", async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);

    const npm = screen.getByRole("combobox", {
      name: t("setup.intelligence.environment.declaration.npm"),
    });
    await userEvent.type(npm, "typescript@5.6.3{enter}");

    expect(onChange).toHaveBeenLastCalledWith({
      packages: { apt: [], npm: ["typescript@5.6.3"], uv: [] },
      dns: { nameservers: ["1.1.1.1", "8.8.8.8"] },
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
    render(<Harness initial={onHost} onChange={onChange} onValidityChange={onValidityChange} />);

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
