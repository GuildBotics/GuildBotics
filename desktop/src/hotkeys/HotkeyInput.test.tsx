import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import "../i18n";
import { HotkeyInput } from "./HotkeyInput";

const t = i18n.getFixedT("en");

function renderInput(props: Partial<React.ComponentProps<typeof HotkeyInput>> = {}) {
  const onChange = props.onChange ?? vi.fn();
  render(
    <MantineProvider env="test">
      <HotkeyInput value="" isMac {...props} onChange={onChange} />
    </MantineProvider>,
  );
  return { onChange, field: screen.getByLabelText(t("hotkey.label")) };
}

describe("HotkeyInput", () => {
  it("shows the assigned combination in macOS menu notation", () => {
    renderInput({ value: "Control+Alt+G" });

    expect(screen.getByLabelText(t("hotkey.label"))).toHaveValue("⌃⌥G");
  });

  it("prompts for a key press while focused", async () => {
    const user = userEvent.setup();
    const { field } = renderInput({ value: "Control+Alt+G" });

    await user.click(field);

    expect(field).toHaveValue(t("hotkey.recording"));
  });

  it("records the pressed combination and stops recording", async () => {
    const user = userEvent.setup();
    const { onChange, field } = renderInput();

    await user.click(field);
    await user.keyboard("{Control>}{Alt>}g{/Alt}{/Control}");

    expect(onChange).toHaveBeenCalledWith("Control+Alt+G");
    expect(field).not.toHaveFocus();
  });

  it("keeps waiting while only modifiers are held", async () => {
    const user = userEvent.setup();
    const { onChange, field } = renderInput();

    await user.click(field);
    await user.keyboard("{Control>}");

    expect(onChange).not.toHaveBeenCalled();
    expect(field).toHaveValue(t("hotkey.recording"));
  });

  it("refuses a bare key that would be taken from every app", async () => {
    const user = userEvent.setup();
    const { onChange, field } = renderInput();

    await user.click(field);
    await user.keyboard("g");

    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByText(t("hotkey.needsModifier"))).toBeInTheDocument();
  });

  it("clears the assignment on an unmodified Backspace", async () => {
    const user = userEvent.setup();
    const { onChange, field } = renderInput({ value: "Control+Alt+G" });

    await user.click(field);
    await user.keyboard("{Backspace}");

    expect(onChange).toHaveBeenCalledWith("");
  });

  it("leaves the assignment untouched when recording is cancelled", async () => {
    const user = userEvent.setup();
    const { onChange, field } = renderInput({ value: "Control+Alt+G" });

    await user.click(field);
    await user.keyboard("{Escape}");

    expect(onChange).not.toHaveBeenCalled();
    expect(field).toHaveValue("⌃⌥G");
  });

  it("reports recording so callers can release the registered shortcuts", async () => {
    const user = userEvent.setup();
    const onRecordingChange = vi.fn();
    const { field } = renderInput({ onRecordingChange });

    await user.click(field);
    expect(onRecordingChange).toHaveBeenLastCalledWith(true);

    await user.keyboard("{Control>}{Alt>}g{/Alt}{/Control}");
    expect(onRecordingChange).toHaveBeenLastCalledWith(false);
  });
});
