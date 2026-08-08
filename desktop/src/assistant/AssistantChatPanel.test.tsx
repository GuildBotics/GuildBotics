import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { AssistantChatPanel, type AssistantChatPanelProps } from "./AssistantChatPanel";
import i18n from "../i18n";
import "../i18n";

const t = i18n.getFixedT("en");

function renderPanel(overrides: Partial<AssistantChatPanelProps> = {}) {
  const props: AssistantChatPanelProps = {
    namespace: "diagnostics.troubleshooting",
    messages: [],
    pending: false,
    disabled: false,
    error: null,
    onSubmit: vi.fn(),
    ...overrides,
  };
  render(
    <MemoryRouter>
      <MantineProvider env="test">
        <AssistantChatPanel {...props} />
      </MantineProvider>
    </MemoryRouter>,
  );
  return props;
}

describe("AssistantChatPanel", () => {
  it("labels itself from the namespace it is given", () => {
    renderPanel({ namespace: "commands.authoring" });

    expect(screen.getByRole("region", { name: t("commands.authoring.title") })).toBeInTheDocument();
    expect(screen.getByText(t("commands.authoring.empty"))).toBeInTheDocument();
  });

  it("submits the trimmed message and clears the input", async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderPanel();

    await user.type(
      screen.getByLabelText(t("diagnostics.troubleshooting.inputLabel")),
      "  why did it fail?  ",
    );
    await user.click(screen.getByRole("button", { name: t("diagnostics.troubleshooting.send") }));

    expect(onSubmit).toHaveBeenCalledWith("why did it fail?");
    expect(screen.getByLabelText(t("diagnostics.troubleshooting.inputLabel"))).toHaveValue("");
  });

  it("does not submit while a turn is pending", async () => {
    const user = userEvent.setup();
    const { onSubmit } = renderPanel({ pending: true, messages: [] });

    expect(screen.getByLabelText(t("diagnostics.troubleshooting.inputLabel"))).toBeDisabled();
    expect(screen.getByText(t("diagnostics.troubleshooting.thinking"))).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: t("diagnostics.troubleshooting.send") }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("keeps the send button disabled without a member", () => {
    renderPanel({ disabled: true });

    expect(
      screen.getByRole("button", { name: t("diagnostics.troubleshooting.send") }),
    ).toBeDisabled();
  });

  it("shows the error and gives the failed message back to the user", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    const panel = (error: string | null) => (
      <MemoryRouter>
        <MantineProvider env="test">
          <AssistantChatPanel
            namespace="diagnostics.troubleshooting"
            messages={[]}
            pending={false}
            disabled={false}
            error={error}
            onSubmit={onSubmit}
          />
        </MantineProvider>
      </MemoryRouter>
    );
    const { rerender } = render(panel(null));
    const input = screen.getByLabelText(t("diagnostics.troubleshooting.inputLabel"));

    await user.type(input, "why did it fail?");
    await user.click(screen.getByRole("button", { name: t("diagnostics.troubleshooting.send") }));
    expect(input).toHaveValue("");

    rerender(panel("The assistant is unavailable."));

    expect(screen.getByText("The assistant is unavailable.")).toBeInTheDocument();
    expect(screen.getByText(t("diagnostics.troubleshooting.errorTitle"))).toBeInTheDocument();
    // Retyping a long question after a transient failure would be miserable.
    expect(screen.getByLabelText(t("diagnostics.troubleshooting.inputLabel"))).toHaveValue(
      "why did it fail?",
    );
  });

  it("renders cited traces as links into the executions view", () => {
    renderPanel({
      messages: [
        {
          role: "assistant",
          content: "The token expired.",
          traceId: "turn-1",
          references: [
            { traceId: "abc123", label: "git/push", to: "/diagnostics?trace_id=abc123" },
          ],
        },
      ],
    });

    const link = screen.getByRole("link", { name: "git/push" });
    expect(link).toHaveAttribute("href", "/diagnostics?trace_id=abc123");
    expect(link).toHaveAttribute("title", "abc123");
  });

  it("shows live progress supplied by the caller while pending", () => {
    renderPanel({ pending: true, progress: <span>reading trace abc123</span> });

    expect(screen.getByText("reading trace abc123")).toBeInTheDocument();
  });

  it("scrolls to a newly appended assistant response only", () => {
    const scrollIntoView = vi.spyOn(Element.prototype, "scrollIntoView");
    const panel = (messages: AssistantChatPanelProps["messages"]) => (
      <MemoryRouter>
        <MantineProvider env="test">
          <AssistantChatPanel
            namespace="commands.authoring"
            messages={messages}
            pending={false}
            disabled={false}
            error={null}
            onSubmit={vi.fn()}
            autoScrollOnAssistantResponse
          />
        </MantineProvider>
      </MemoryRouter>
    );
    const userMessage: AssistantChatPanelProps["messages"][number] = {
      role: "user",
      content: "Polish this command.",
    };
    const { rerender } = render(panel([]));

    rerender(panel([userMessage]));
    expect(scrollIntoView).not.toHaveBeenCalled();

    rerender(panel([userMessage, { role: "assistant", content: "I updated the command." }]));
    expect(scrollIntoView).toHaveBeenCalledOnce();
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "end" });

    scrollIntoView.mockRestore();
  });
});
