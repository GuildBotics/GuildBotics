import { afterEach, expect, it, vi } from "vitest";
import { createApiClient } from "./api";

const error = async (response: Response) =>
  Object.assign(new Error("refused"), { code: (await response.json()).code as string });
const client = (prefix: string, csrfHeader: string) =>
  createApiClient({ prefix, csrfHeader, readError: error });
afterEach(() => vi.unstubAllGlobals());

it("keeps the two apps' session tokens and headers isolated, including multipart uploads", async () => {
  const fetch = vi.fn().mockImplementation(async () => new Response("{}"));
  vi.stubGlobal("fetch", fetch);
  const desktop = client("/api", "X-GuildBotics-CSRF");
  const branvil = client("/api/v1", "X-Branvil-CSRF");
  desktop.setCsrf("desktop-fixture");
  branvil.setCsrf("branvil-fixture");
  const body = new FormData();
  body.append("file", new Blob(["avatar"]));
  await desktop.send("/avatar", { method: "POST", body });
  await branvil.send("/ticket", { method: "POST", body: "{}" });
  expect(fetch.mock.calls[0]).toEqual([
    "/api/avatar",
    {
      method: "POST",
      body,
      credentials: "same-origin",
      headers: { "X-GuildBotics-CSRF": "desktop-fixture" },
    },
  ]);
  expect(fetch.mock.calls[1][1].headers).toEqual({ "X-Branvil-CSRF": "branvil-fixture" });
});

it("replays a CSRF-refused write once with the same body and idempotency key", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(new Response('{"code":"csrf_mismatch"}', { status: 403 }))
    .mockResolvedValueOnce(new Response('{"csrf":"new-fixture"}'))
    .mockResolvedValueOnce(new Response('{"code":"csrf_mismatch"}', { status: 403 }));
  vi.stubGlobal("fetch", fetch);
  const api = client("/api", "X-CSRF");
  await expect(
    api.send("/x", { method: "POST", body: "{}", headers: { "Idempotency-Key": "one-create" } }),
  ).rejects.toMatchObject({ code: "csrf_mismatch" });
  expect(fetch).toHaveBeenCalledTimes(3);
  expect(fetch.mock.calls[2][1]).toMatchObject({
    body: "{}",
    headers: { "X-CSRF": "new-fixture", "Idempotency-Key": "one-create" },
  });
});

it.each(["GET", "HEAD"])("never refreshes or replays a %s refusal", async (method) => {
  const fetch = vi
    .fn()
    .mockResolvedValue(new Response('{"code":"csrf_mismatch"}', { status: 403 }));
  vi.stubGlobal("fetch", fetch);
  await expect(client("/api", "X-CSRF").send("/x", { method })).rejects.toMatchObject({
    code: "csrf_mismatch",
  });
  expect(fetch).toHaveBeenCalledTimes(1);
});
