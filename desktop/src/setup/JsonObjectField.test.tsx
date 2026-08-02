import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { JsonObjectField } from "./JsonObjectField";

function Harness({
  initial = {},
  onChange,
  onValidityChange,
}: {
  initial?: Record<string, unknown>;
  onChange?: (value: Record<string, unknown>) => void;
  onValidityChange?: (valid: boolean) => void;
}) {
  const [value, setValue] = useState<Record<string, unknown>>(initial);
  return (
    <MantineProvider>
      <JsonObjectField
        label="Settings"
        errorText="Enter a JSON object."
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

describe("JsonObjectField", () => {
  it("shows the current value as formatted JSON", () => {
    render(<Harness initial={{ high: { budget: 8000 } }} />);
    expect(screen.getByLabelText("Settings")).toHaveValue(
      JSON.stringify({ high: { budget: 8000 } }, null, 2),
    );
  });

  it("reports edited values with numbers, booleans and nesting intact", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);

    const field = screen.getByLabelText("Settings");
    await user.clear(field);
    await user.click(field);
    await user.paste('{"high":{"thinking":{"enabled":true,"budget":8000}}}');

    expect(onChange).toHaveBeenLastCalledWith({
      high: { thinking: { enabled: true, budget: 8000 } },
    });
  });

  it("keeps malformed text on screen, reports it, and withholds the change", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const onValidityChange = vi.fn();
    render(<Harness onChange={onChange} onValidityChange={onValidityChange} />);

    const field = screen.getByLabelText("Settings");
    await user.clear(field);
    onChange.mockClear();
    await user.click(field);
    await user.paste("{not json");

    expect(screen.getByText("Enter a JSON object.")).toBeInTheDocument();
    expect(field).toHaveValue("{not json");
    expect(onChange).not.toHaveBeenCalled();
    expect(onValidityChange).toHaveBeenLastCalledWith(false);
  });

  it("rejects JSON that is not an object", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);

    const field = screen.getByLabelText("Settings");
    await user.clear(field);
    onChange.mockClear();
    await user.click(field);
    await user.paste('["high"]');

    expect(screen.getByText("Enter a JSON object.")).toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("reports valid again when it unmounts while showing an error", async () => {
    // Otherwise the caller keeps blocking saves on an error whose field is gone,
    // with nothing on screen to correct.
    const user = userEvent.setup();
    const onValidityChange = vi.fn();
    const { unmount } = render(<Harness onValidityChange={onValidityChange} />);

    const field = screen.getByLabelText("Settings");
    await user.clear(field);
    await user.click(field);
    await user.paste("{not json");
    expect(onValidityChange).toHaveBeenLastCalledWith(false);

    unmount();

    expect(onValidityChange).toHaveBeenLastCalledWith(true);
  });

  it("treats empty text as an empty object", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const onValidityChange = vi.fn();
    render(
      <Harness initial={{ high: {} }} onChange={onChange} onValidityChange={onValidityChange} />,
    );

    await user.clear(screen.getByLabelText("Settings"));

    expect(onChange).toHaveBeenLastCalledWith({});
    expect(onValidityChange).toHaveBeenLastCalledWith(true);
  });
});
