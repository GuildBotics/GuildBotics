import { describe, expect, it } from "vitest";
import { ApiError, enc, getAll, getCsrfToken, request, setCsrfToken } from "../api/client";
import * as api from "../api/endpoints";
import { apiError, fakeApi, json } from "../test/fakeApi";

describe("API client", () => {
  it("sends the CSRF header on non-GET requests only, with same-origin credentials", async () => {
    const fake = fakeApi({
      "GET /api/v1/me": json(200, { name: "owner" }),
      "POST /api/v1/repos/acme/demo/tickets/1/comments": json(201, { id: "c1" }),
      "DELETE /api/v1/members/aiko/tokens/tok_1": { status: 204 },
    });
    setCsrfToken("csrf-xyz");
    await api.me();
    await api.tickets.addComment("acme/demo", "1", "hello");
    await api.members.revokeToken("aiko", "tok_1");

    const [getReq, postReq, deleteReq] = fake.requests;
    expect(getReq?.headers["X-Branvil-CSRF"]).toBeUndefined();
    expect(postReq?.headers["X-Branvil-CSRF"]).toBe("csrf-xyz");
    expect(postReq?.headers["Content-Type"]).toBe("application/json");
    expect(postReq?.headers["Idempotency-Key"]).toMatch(/^[0-9a-f-]{36}$/);
    expect(postReq?.body).toEqual({ body: "hello" });
    expect(deleteReq?.headers["X-Branvil-CSRF"]).toBe("csrf-xyz");
    for (const r of fake.requests) {
      expect(r.credentials).toBe("same-origin");
      expect(r.headers["Authorization"]).toBeUndefined();
    }
  });

  it("encodes path parameters as one segment", async () => {
    const fake = fakeApi({ "GET /api/v1/repos/acme/demo": json(200, {}) });
    await api.repos.get("acme/demo");
    expect(enc("acme/demo")).toBe("acme%2Fdemo");
    // The raw URL keeps the slashes encoded.
    const fetchMock = globalThis.fetch as unknown as { mock: { calls: [string][] } };
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/repos/acme%2Fdemo");
    expect(fake.requests).toHaveLength(1);
  });

  it("turns error bodies into ApiError with status, code, and message", async () => {
    fakeApi({ "POST /api/v1/x": apiError(409, "head_moved", "The head moved.") });
    const err = await request("POST", "/x", { body: {} }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 409, code: "head_moved", message: "The head moved." });
  });

  it.each(["POST", "PATCH", "PUT", "DELETE"])("refreshes CSRF and retries %s once with the same payload, query and key", async (method) => {
    const fake = fakeApi({
      [`${method} /api/v1/x`]: [apiError(403, "csrf_mismatch", "Old session."), json(200, { saved: true })],
      "GET /api/v1/session": json(200, { csrf: "fresh" }),
    });
    setCsrfToken("old");
    await expect(request(method, "/x", { body: { text: "hello" }, query: { page: 2 }, idempotencyKey: "same-key" })).resolves.toEqual({ saved: true });
    expect(fake.requests.map((r) => `${r.method} ${r.path}`)).toEqual([`${method} /api/v1/x`, "GET /api/v1/session", `${method} /api/v1/x`]);
    const [first, refresh, retry] = fake.requests;
    expect(first?.headers["X-Branvil-CSRF"]).toBe("old");
    expect(refresh?.headers["X-Branvil-CSRF"]).toBeUndefined();
    expect(retry).toEqual({ ...first, headers: { ...first?.headers, "X-Branvil-CSRF": "fresh" } });
    expect(getCsrfToken()).toBe("fresh");
  });

  it("replays an uploaded Blob without changing its content type", async () => {
    const fake = fakeApi({
      "PUT /api/v1/picture": [apiError(403, "csrf_mismatch", "Old session."), { status: 204 }],
      "GET /api/v1/session": json(200, { csrf: "fresh" }),
    });
    const file = new Blob(["image"], { type: "image/png" });
    await expect(request("PUT", "/picture", { body: file })).resolves.toBeUndefined();
    expect(fake.requests[2]?.body).toBe(file);
    expect(fake.requests[2]?.headers["Content-Type"]).toBe("image/png");
  });

  it.each(["csrf_mismatch", "unauthorized"])("stops after one retry when the write is refused again with %s", async (code) => {
    const fake = fakeApi({
      "POST /api/v1/x": [apiError(403, "csrf_mismatch", "Old session."), apiError(code === "unauthorized" ? 401 : 403, code, "Refused again.")],
      "GET /api/v1/session": json(200, { csrf: "fresh" }),
    });
    await expect(request("POST", "/x")).rejects.toMatchObject({ code: "session_changed" });
    expect(fake.calls("POST", "/api/v1/x")).toHaveLength(2);
    expect(fake.calls("GET", "/api/v1/session")).toHaveLength(1);
    expect(getCsrfToken()).toBeNull();
  });

  it("does not replay the write if refreshing the session is refused", async () => {
    const fake = fakeApi({
      "POST /api/v1/x": apiError(403, "csrf_mismatch", "Old session."),
      "GET /api/v1/session": apiError(401, "unauthorized", "Expired."),
    });
    await expect(request("POST", "/x")).rejects.toMatchObject({ code: "session_changed" });
    expect(fake.requests).toHaveLength(2);
  });

  it.each([[403, "forbidden"], [401, "unauthorized"], [500, "internal"]])("does not retry an ordinary %s %s failure", async (status, code) => {
    const fake = fakeApi({ "POST /api/v1/x": apiError(Number(status), String(code), "Refused.") });
    await expect(request("POST", "/x")).rejects.toMatchObject({ code });
    expect(fake.requests).toHaveLength(1);
  });

  it("preserves cancellation during the session refresh and never replays the write", async () => {
    const controller = new AbortController();
    const fake = fakeApi({
      "POST /api/v1/x": apiError(403, "csrf_mismatch", "Old session."),
      "GET /api/v1/session": () => {
        controller.abort();
        throw new DOMException("Aborted", "AbortError");
      },
    });
    await expect(request("POST", "/x", { signal: controller.signal })).rejects.toMatchObject({ name: "AbortError" });
    expect(fake.requests).toHaveLength(2);
    expect(globalThis.fetch).toHaveBeenLastCalledWith("/api/v1/session", expect.objectContaining({ signal: controller.signal }));
  });

  it("follows next_cursor to the end of a collection", async () => {
    const fake = fakeApi({
      "GET /api/v1/repos": [json(200, { items: [{ path: "a/b" }], next_cursor: "c1" }), json(200, { items: [{ path: "a/c" }], next_cursor: null })],
    });
    const all = await getAll<{ path: string }>("/repos");
    expect(all).toEqual({ items: [{ path: "a/b" }, { path: "a/c" }], complete: true });
    expect(fake.requests[1]?.search).toContain("cursor=c1");
  });
});
