import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import type { EffortFieldSpec, EffortOverlay } from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { EffortSettingsField, ToolSettingsField } from "./EffortSettingsField";

const t = i18n.getFixedT("en");

/** `low` and `high`; `default` can never be mapped. */
const EDITABLE_LEVEL_COUNT = 2;

/** Anthropic's shape: a nested object holding both an enum and an integer. */
const NESTED_FIELDS: EffortFieldSpec[] = [
  {
    key: "thinking.type",
    type: "enum",
    values: ["enabled", "disabled"],
    minimum: null,
    maximum: null,
  },
  {
    key: "thinking.budget_tokens",
    type: "integer",
    values: [],
    minimum: 1024,
    maximum: null,
  },
];

function Harness({
  initial = {},
  inherited = {},
  fields = NESTED_FIELDS,
  supported,
  onChange,
}: {
  initial?: EffortOverlay;
  inherited?: EffortOverlay;
  fields?: EffortFieldSpec[];
  supported?: boolean;
  onChange?: (value: EffortOverlay) => void;
}) {
  const [value, setValue] = useState<EffortOverlay>(initial);
  return (
    <MantineProvider>
      <EffortSettingsField
        value={value}
        inherited={inherited}
        fields={fields}
        supported={supported}
        onChange={(next) => {
          setValue(next);
          onChange?.(next);
        }}
      />
    </MantineProvider>
  );
}

describe("EffortSettingsField", () => {
  it("shows what an unset mapping inherits, without an input to fill in", () => {
    render(
      <Harness inherited={{ high: { thinking: { type: "enabled", budget_tokens: 8000 } } }} />,
    );

    expect(screen.getByText(t("setup.intelligence.effort.inherited"))).toBeInTheDocument();
    expect(screen.getByText(/thinking.type = enabled/)).toBeInTheDocument();
    expect(screen.getByText(/thinking.budget_tokens = 8000/)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("offers no row for `default`, which no mapping could ever apply", () => {
    // `default` means "do not intervene". It is a meaningful request, but not
    // something a provider's settings can describe, so showing it here would
    // invite a mapping the runtime silently ignores.
    render(<Harness />);
    expect(screen.queryByText(/^default:/)).not.toBeInTheDocument();
    expect(screen.getByText(/^low:/)).toBeInTheDocument();
    expect(screen.getByText(/^high:/)).toBeInTheDocument();
  });

  it("edits a nested integer without stringifying it", async () => {
    // The backend merges these straight into the provider parameters, where a
    // stringified budget is rejected.
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);

    await user.click(
      screen.getByRole("button", { name: t("setup.intelligence.effort.customize") }),
    );
    await user.type(screen.getByLabelText("high thinking.budget_tokens"), "8000");

    expect(onChange).toHaveBeenLastCalledWith({
      high: { thinking: { budget_tokens: 8000 } },
    });
  });

  it("writes an enum into its nested position", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);

    await user.click(
      screen.getByRole("button", { name: t("setup.intelligence.effort.customize") }),
    );
    await user.click(screen.getByLabelText("low thinking.type"));
    await user.click(await screen.findByRole("option", { name: "disabled" }));

    expect(onChange).toHaveBeenLastCalledWith({ low: { thinking: { type: "disabled" } } });
  });

  it("keeps the rest of an inherited mapping when the first edit is made", async () => {
    // Editing starts from what was on screen, so customizing one level does not
    // silently drop the inherited settings of the other.
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <Harness
        inherited={{
          low: { thinking: { type: "disabled" } },
          high: { thinking: { type: "enabled", budget_tokens: 8000 } },
        }}
        onChange={onChange}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: t("setup.intelligence.effort.customize") }),
    );
    const budget = screen.getByLabelText("high thinking.budget_tokens");
    await user.clear(budget);
    await user.type(budget, "2048");

    expect(onChange).toHaveBeenLastCalledWith({
      low: { thinking: { type: "disabled" } },
      high: { thinking: { type: "enabled", budget_tokens: 2048 } },
    });
  });

  it("removes an emptied nested object rather than leaving it behind", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <Harness initial={{ high: { thinking: { budget_tokens: 8000 } } }} onChange={onChange} />,
    );

    await user.clear(screen.getByLabelText("high thinking.budget_tokens"));

    expect(onChange).toHaveBeenLastCalledWith({});
  });

  it("names a model_id field by what it does, not by the provider's key", async () => {
    // Providers call this `id` or `model`; neither says it swaps the model for
    // one level, and `id` reads as a duplicate of the slot's own model field.
    const user = userEvent.setup();
    render(
      <Harness
        fields={[{ key: "id", type: "model_id", values: [], minimum: null, maximum: null }]}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: t("setup.intelligence.effort.customize") }),
    );

    expect(
      screen.getByLabelText(`high ${t("setup.intelligence.effort.modelLevelLabel")}`),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("high id")).not.toBeInTheDocument();
    // Empty means "this level changes nothing", not "unset".
    expect(
      screen.getAllByPlaceholderText(t("setup.intelligence.effort.emptyChangesNothing")),
    ).toHaveLength(EDITABLE_LEVEL_COUNT);
  });

  it("explains instead of offering an editor when the tool cannot apply any", async () => {
    // Grok Build's protocol has nowhere to put these, so its adapter drops them
    // with a warning. Collecting them anyway would be dead configuration.
    render(<Harness fields={[]} supported={false} />);

    expect(screen.getByText(t("setup.intelligence.effort.unsupported"))).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("setup.intelligence.effort.customize") }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("falls back to JSON when the provider describes no settings", async () => {
    const user = userEvent.setup();
    render(<Harness fields={[]} />);

    await user.click(
      screen.getByRole("button", { name: t("setup.intelligence.effort.customize") }),
    );

    expect(screen.getByLabelText(t("setup.intelligence.effortJson"))).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: t("setup.intelligence.effort.showJson") }),
    ).not.toBeInTheDocument();
  });

  it("offers JSON as an escape hatch when settings are described", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(
      screen.getByRole("button", { name: t("setup.intelligence.effort.customize") }),
    );
    expect(screen.queryByLabelText(t("setup.intelligence.effortJson"))).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: t("setup.intelligence.effort.showJson") }));
    expect(screen.getByLabelText(t("setup.intelligence.effortJson"))).toBeInTheDocument();
  });
});

describe("ToolSettingsField", () => {
  const MODEL_FIELD: EffortFieldSpec[] = [
    { key: "id", type: "model_id", values: [], minimum: null, maximum: null },
    { key: "effort", type: "enum", values: ["low", "high"], minimum: null, maximum: null },
  ];

  function BaselineHarness({ onChange }: { onChange: (value: Record<string, unknown>) => void }) {
    const [value, setValue] = useState<Record<string, unknown>>({});
    return (
      <MantineProvider>
        <ToolSettingsField
          value={value}
          fields={MODEL_FIELD}
          onChange={(next) => {
            setValue(next);
            onChange(next);
          }}
        />
      </MantineProvider>
    );
  }

  it("edits settings that apply whatever effort was asked for", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<BaselineHarness onChange={onChange} />);

    await user.type(
      screen.getByLabelText(`always ${t("setup.intelligence.effort.modelAlwaysLabel")}`),
      "steady",
    );

    expect(onChange).toHaveBeenLastCalledWith({ id: "steady" });
  });

  it("says an empty value defers to the tool, for every field alike", () => {
    // `model` and `effort` mean the same by empty here, so wording that came
    // from the field type made them disagree for no reason.
    render(<BaselineHarness onChange={vi.fn()} />);

    expect(
      screen.getAllByPlaceholderText(t("setup.intelligence.effort.emptyIsToolDefault")),
    ).toHaveLength(MODEL_FIELD.length);
  });
});
