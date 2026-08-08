import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MemberSelector } from "./MemberSelector";

describe("MemberSelector", () => {
  it("selects a member when no member is currently resolved", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <MantineProvider env="test">
        <MemberSelector
          ariaLabel="Choose member"
          member={null}
          members={[{ person_id: "aiko", name: "Aiko" }]}
          onChange={onChange}
        />
      </MantineProvider>,
    );

    await user.click(screen.getByRole("button", { name: "Choose member" }));
    await user.click(await screen.findByRole("menuitem", { name: "Aiko" }));

    expect(onChange).toHaveBeenCalledWith("aiko");
  });

  it("is disabled only when there are no selectable members", () => {
    render(
      <MantineProvider env="test">
        <MemberSelector ariaLabel="Choose member" member={null} members={[]} onChange={vi.fn()} />
      </MantineProvider>,
    );

    expect(screen.getByRole("button", { name: "Choose member" })).toBeDisabled();
  });
});
