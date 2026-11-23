import { afterEach, expect, it, vi } from "vitest";
import { rememberReturn, takeReturn } from "./entryReturn";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

it("remembers the consent page once with an expiry", () => {
  const path = "/authorize?client=app";
  window.history.replaceState(null, "", path);
  rememberReturn();
  expect(takeReturn()).toBe(path);
  expect(takeReturn()).toBeNull();
  rememberReturn();
  vi.spyOn(Date, "now").mockReturnValue(Date.now() + 600_001);
  expect(takeReturn()).toBeNull();
});

it.each([
  "//evil/",
  "/elsewhere",
  "/-/authorize?client=app",
  "/authorize\\?x=evil",
  "/authorize?x=\n",
  "/authorize?x=#fragment",
])("refuses unsafe or non-consent destination %j", (path) => {
  localStorage.setItem(
    "guildbotics.entryReturn",
    JSON.stringify({ path, expires: Date.now() + 60_000 }),
  );
  expect(takeReturn()).toBeNull();
  expect(localStorage.getItem("guildbotics.entryReturn")).toBeNull();
});

it("keeps opaque query values", () => {
  const path = "/authorize?state=a\\b";
  window.history.replaceState(null, "", path);
  rememberReturn();
  expect(takeReturn()).toBe(path);
});

it("tolerates malformed or unavailable storage", () => {
  for (const raw of ["{", "null", "{}"]) {
    localStorage.setItem("guildbotics.entryReturn", raw);
    expect(takeReturn()).toBeNull();
  }
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("unavailable");
  });
  expect(takeReturn()).toBeNull();
});
