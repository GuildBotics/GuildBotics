import { MantineProvider } from "@mantine/core";
import DOMPurify from "dompurify";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { Markdown, renderMarkdown } from "../components/Markdown";
import { I18nProvider, dictionaries } from "../i18n";
import { keys } from "../query/keys";
import { applyChange } from "../query/events";
import { fakeApi, json, apiError } from "../test/fakeApi";
import examples from "../../../tests/fixtures/markdown-references.json";
import { drawDiagram } from "../components/diagram";

// Mermaid needs a layout engine to measure text, which jsdom does not have.
vi.mock("../components/diagram", () => ({ drawDiagram: vi.fn() }));

const endpoint = "/api/v1/repos/acme/demo/markdown";

function markdownView(source: string, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  const view = (text: string) => <MantineProvider env="test"><QueryClientProvider client={client}><I18nProvider initial="ja"><Markdown repo="acme/demo" source={text} /></I18nProvider></QueryClientProvider></MantineProvider>;
  return { ...render(view(source)), client, view };
}

describe("Markdown rendering", () => {
  it("renders Markdown", () => {
    const html = renderMarkdown("# Title\n\n**bold** and `code`");
    expect(html).toContain("<h1>Title</h1>");
    expect(html).toContain("<strong>bold</strong>");
    expect(html).toContain("<code>code</code>");
  });

  it("removes scripts, event handlers, and dangerous URLs", () => {
    const html = renderMarkdown(
      [
        "<script>alert(1)</script>",
        '<img src="x" onerror="alert(2)">',
        "[click](javascript:alert(3))",
        '<a href="data:text/html,<script>alert(4)</script>">d</a>',
        '<iframe src="https://example.com"></iframe>',
        '<p style="position:fixed" onclick="alert(5)">p</p>',
        '<form action="/api/v1/session"><input name="x"></form>',
      ].join("\n\n"),
    );
    expect(html).not.toMatch(/<script/i);
    expect(html).not.toMatch(/onerror|onclick/i);
    expect(html).not.toMatch(/javascript:/i);
    expect(html).not.toMatch(/data:text/i);
    expect(html).not.toMatch(/<iframe|<form|<input|style=/i);
  });

  it("keeps a GFM table and its column alignment", () => {
    const html = renderMarkdown(
      ["| Left | Center | Right |", "| :--- | :---: | ---: |", "| a | b | c |"].join("\n"),
    );
    expect(html).toContain("<table>");
    expect(html).toContain("<thead>");
    expect(html).toContain("<tbody>");
    expect(html).toMatch(/<th align="left">Left<\/th>/);
    expect(html).toMatch(/<th align="center">Center<\/th>/);
    expect(html).toMatch(/<th align="right">Right<\/th>/);
    expect(html).toMatch(/<td align="left">a<\/td>/);
    expect(html).toMatch(/<td align="center">b<\/td>/);
    expect(html).toMatch(/<td align="right">c<\/td>/);
  });

  it("opens links in a new context without referrer or opener", async () => {
    const source = "[docs](https://example.com/x)";
    fakeApi({ [`POST ${endpoint}`]: json(200, { markdown: source }) });
    const { container, client } = markdownView(source);
    await waitFor(() => expect(client.getQueryData(keys.markdown("acme/demo", source))).toBeDefined());
    const a = container.querySelector("a");
    expect(a?.getAttribute("href")).toBe("https://example.com/x");
    expect(a?.getAttribute("rel")).toBe("noopener noreferrer nofollow");
    expect(a?.getAttribute("target")).toBe("_blank");
  });

  it.each(examples.cases)("displays the service's shared example: $name", async (example) => {
    const fake = fakeApi({ [`POST ${endpoint}`]: json(200, { markdown: example.markdown }) });
    const { container, client } = markdownView(example.source);
    await waitFor(() => expect(client.getQueryData(keys.markdown("acme/demo", example.source))).toEqual({ markdown: example.markdown }));
    const links = [...container.querySelectorAll<HTMLAnchorElement>("a[data-branvil-kind]")];
    expect(links).toHaveLength(example.links.length);
    for (const [i, expected] of example.links.entries()) {
      const target = examples.targets[expected.target]!;
      const a = links[i]!;
      expect(a.textContent).toBe(expected.label);
      expect(a.href).toBe(`${examples.base_url}/${target.repository}/-/${target.kind === "ticket" ? "tickets" : "pulls"}/${target.number}`);
      expect(a.dataset.branvilKind).toBe(target.kind);
      expect(a.dataset.branvilState).toBe(target.state);
      expect(a.title).toContain(target.title);
      expect(a.title).toContain(dictionaries.ja[`markdown.${target.kind as "ticket" | "pull"}`]);
      expect(a.getAttribute("aria-label")).toContain(a.title);
      expect(a.rel).toBe("noopener noreferrer nofollow");
    }
    expect(container.querySelector("code a, pre a, a a")).toBeNull();
    expect(container.querySelector('a[href*="999"]')).toBeNull();
    expect(fake.calls("POST", endpoint)[0]?.body).toEqual({ source: example.source });
    expect(fake.calls("POST", endpoint)[0]?.headers["Idempotency-Key"]).toBeUndefined();
  });

  it("sanitizes the service's result, including hostile reference titles", async () => {
    const safe = examples.cases[0]!.markdown;
    fakeApi({ [`POST ${endpoint}`]: json(200, { markdown: `${safe}\n\n<img src="https://example.com/x" onerror="alert(1)"><script>alert(2)</script><a href="javascript:alert(3)" data-branvil-kind="ticket" data-branvil-state="open">evil</a>` }) });
    const { container, client } = markdownView("#1");
    await waitFor(() => expect(client.getQueryData(keys.markdown("acme/demo", "#1"))).toBeDefined());
    expect(container.innerHTML).not.toMatch(/<script|onerror|javascript:/i);
    expect(container.querySelector<HTMLAnchorElement>('a[data-branvil-kind="ticket"]')?.title).toContain('Ticket <one> " &');
    expect(container.querySelector("one")).toBeNull();
  });

  it("refreshes references to another repository after its target changes", async () => {
    const source = "acme/other#1";
    const open = `<a href="/acme/other/-/tickets/1" data-branvil-kind="ticket" data-branvil-state="open" title="Other">acme/other#1</a>`;
    fakeApi({ [`POST ${endpoint}`]: [json(200, { markdown: open }), json(200, { markdown: open.replace('state="open"', 'state="closed"') })] });
    const { container, client } = markdownView(source);
    await waitFor(() => expect(container.querySelector("a")?.dataset.branvilState).toBe("open"));
    act(() => applyChange({ kind: "ticket", path: "acme/other", id: "1" }, client));
    await waitFor(() => expect(container.querySelector("a")?.dataset.branvilState).toBe("closed"));
    expect(container.querySelector("a")?.title).toContain("クローズ");
  });

  it("does not put a late result into an edited body", async () => {
    let answer!: (value: ReturnType<typeof json>) => void;
    fakeApi({ [`POST ${endpoint}`]: (req) => (req.body as { source: string }).source === "old #1"
      ? new Promise((resolve) => { answer = resolve; }) : json(200, { markdown: "new !999" }) });
    const { container, rerender, view, client } = markdownView("old #1");
    await waitFor(() => expect(answer).toBeTypeOf("function"));
    rerender(view("new !999"));
    await waitFor(() => expect(client.getQueryData(keys.markdown("acme/demo", "new !999"))).toBeDefined());
    await act(async () => answer(json(200, { markdown: examples.cases[0]!.markdown })));
    expect(container).toHaveTextContent("new !999");
    expect(container.querySelector("a")).toBeNull();
  });

  it("keeps the body readable if resolution fails", async () => {
    fakeApi({ [`POST ${endpoint}`]: apiError(500, "internal", "Unavailable") });
    const { container, client } = markdownView("**body** #1");
    await waitFor(() => expect(client.getQueryState(keys.markdown("acme/demo", "**body** #1"))?.status).toBe("error"));
    expect(container.querySelector("strong")).toHaveTextContent("body");
    expect(container).toHaveTextContent("#1");
    expect(container.querySelector("a")).toBeNull();
  });
});

describe("Mermaid diagrams", () => {
  const diagram = "```mermaid\nflowchart LR\n  A[<b>a</b>] --> B\n```";

  it("keeps a Mermaid block as escaped source, marked for drawing", () => {
    const html = renderMarkdown(`${diagram}\n\n\`\`\`ts\nconst x = 1;\n\`\`\``);
    expect(html).toContain('<pre data-mermaid=""><code>flowchart LR\n  A[&lt;b&gt;a&lt;/b&gt;] --&gt; B</code></pre>');
    expect(html).toContain("<pre><code>const x = 1;");
  });

  it("shows the drawn diagram with its source one click away", async () => {
    vi.mocked(drawDiagram).mockResolvedValue({ src: "data:image/svg+xml,<svg/>", alt: "承認の流れ" });
    fakeApi({ [`POST ${endpoint}`]: json(200, { markdown: diagram }) });
    const { container } = markdownView(diagram);
    const image = await screen.findByRole("img", { name: "承認の流れ" });
    expect(image).toHaveAttribute("src", "data:image/svg+xml,<svg/>");
    expect(drawDiagram).toHaveBeenCalledWith("flowchart LR\n  A[<b>a</b>] --> B", container.querySelector(".markdown"));
    expect(container.querySelector("figure details summary")).toHaveTextContent("ソースを表示");
    expect(container.querySelector("figure details pre")).toHaveTextContent("flowchart LR");
  });

  it("opens a diagram full size, named 図 when its source gives no title", async () => {
    vi.mocked(drawDiagram).mockResolvedValue({ src: "data:image/svg+xml,<svg/>", alt: "" });
    fakeApi({ [`POST ${endpoint}`]: json(200, { markdown: diagram }) });
    markdownView(diagram);
    const open = await screen.findByRole("button", { name: "図" });
    expect(open).toHaveAttribute("title", "原寸で表示: 図");
    await userEvent.click(open);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("img", { name: "図" })).toHaveAttribute("src", "data:image/svg+xml,<svg/>");
    await userEvent.click(within(dialog).getByRole("button", { name: "閉じる" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("keeps the link protections when another library adds hooks to the shared DOMPurify", () => {
    // What Mermaid does once a diagram's labels are HTML.
    DOMPurify.addHook("afterSanitizeAttributes", (node) => {
      if (node.tagName === "A") node.setAttribute("target", "_top");
    });
    try {
      const html = renderMarkdown('<a href="https://example.com" target="_top">x</a>');
      expect(html).toContain('target="_blank"');
      expect(html).toContain('rel="noopener noreferrer nofollow"');
    } finally {
      DOMPurify.removeAllHooks();
    }
  });

  it("draws every diagram even when the body imitates a drawn one", async () => {
    vi.mocked(drawDiagram).mockResolvedValue({ src: "data:image/svg+xml,<svg/>", alt: "" });
    const body = `<figure data-diagram=""><pre data-mermaid="">graph TD</pre></figure>\n\n${diagram}`;
    fakeApi({ [`POST ${endpoint}`]: json(200, { markdown: body }) });
    markdownView(body);
    await waitFor(() => expect(screen.getAllByRole("button", { name: "図" })).toHaveLength(2));
  });

  it("redraws in place when the color scheme changes, replacing a failure note without repeating it", async () => {
    vi.mocked(drawDiagram).mockRejectedValueOnce(new Error("first")).mockResolvedValue({ src: "data:image/svg+xml,<svg/>", alt: "" });
    fakeApi({ [`POST ${endpoint}`]: json(200, { markdown: diagram }) });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = (scheme: "light" | "dark") => (
      <MantineProvider env="test" forceColorScheme={scheme}><QueryClientProvider client={client}><I18nProvider initial="ja"><Markdown repo="acme/demo" source={diagram} /></I18nProvider></QueryClientProvider></MantineProvider>
    );
    const { container, rerender } = render(view("light"));
    expect(await screen.findByRole("alert")).toHaveTextContent("first");
    rerender(view("dark"));
    await screen.findByRole("button", { name: "図" });
    expect(screen.queryByRole("alert")).toBeNull();
    rerender(view("light"));
    await waitFor(() => expect(drawDiagram).toHaveBeenCalledTimes(3));
    expect(container.querySelectorAll("figure")).toHaveLength(1);
    expect(container.querySelectorAll("img")).toHaveLength(1);
  });

  it("leaves the source in view with Mermaid's reason when the diagram cannot be drawn", async () => {
    vi.mocked(drawDiagram).mockRejectedValue(new Error("Parse error on line 2"));
    fakeApi({ [`POST ${endpoint}`]: json(200, { markdown: diagram }) });
    const { container } = markdownView(diagram);
    expect(await screen.findByRole("alert")).toHaveTextContent("図を描画できませんでした: Parse error on line 2");
    expect(container.querySelector("figure")).toBeNull();
    expect(container.querySelector("pre[data-mermaid]")).toHaveTextContent("flowchart LR");
  });
});
