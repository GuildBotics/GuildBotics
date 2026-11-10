import { afterEach, expect, it, vi } from "vitest";
import { rememberReturn, takeReturn } from "./entryReturn";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

it.each(["/-/authorize?client=app", "/-/guildbotics/-/authorize?client=app"])(
  "remembers %s once with an expiry",
  (path) => {
    window.history.replaceState(null, "", path);
    rememberReturn();
    expect(takeReturn()).toBe(path);
    expect(takeReturn()).toBeNull();
    rememberReturn();
    vi.spyOn(Date, "now").mockReturnValue(Date.now() + 600_001);
    expect(takeReturn()).toBeNull();
  },
);

it.each([
  "//evil/",
  "/elsewhere",
  "/-/authorize\\?x=evil",
  "/-/authorize?x=\n",
  "/-/authorize?x=#fragment",
])("refuses unsafe or non-consent destination %j", (path) => {
  localStorage.setItem(
    "guildbotics.entryReturn",
    JSON.stringify({ path, expires: Date.now() + 60_000 }),
  );
  expect(takeReturn()).toBeNull();
  expect(localStorage.getItem("guildbotics.entryReturn")).toBeNull();
});

it.each(["/-/authorize?state=a\\b", "/-/guildbotics/-/authorize?state=a\\b"])(
  "keeps opaque query values in %s",
  (path) => {
    window.history.replaceState(null, "", path);
    rememberReturn();
    expect(takeReturn()).toBe(path);
  },
);

it("scopes standalone Branvil and tolerates malformed or unavailable storage", () => {
  for (const raw of [
    "{",
    "null",
    "{}",
    JSON.stringify({ path: "/-/guildbotics/-/authorize?x=1", expires: Date.now() + 60_000 }),
  ]) {
    localStorage.setItem("guildbotics.entryReturn", raw);
    expect(takeReturn(["/-/authorize"])).toBeNull();
  }
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("unavailable");
  });
  expect(takeReturn()).toBeNull();
});
