// Draws a Mermaid diagram as an image. An SVG shown through <img> runs no
// script, loads nothing, and keeps its styles to itself, so the diagram needs
// none of the limits on Markdown's HTML relaxed. Loaded only by pages that
// have a diagram.

import mermaid from "mermaid";

/** Below this, a Gantt chart's date ticks overlap; a wider chart scales down and opens full size. */
const GANTT_WIDTH = 800;

const TOKENS = [
  "--color-text-main",
  "--color-text-muted",
  "--color-bg-panel",
  "--color-bg-panel-subtle",
  "--color-brand",
  "--color-brand-bg-subtle",
  "--color-info",
  "--color-info-bg",
  "--color-warning",
  "--color-danger",
  "--color-success",
] as const;

let drawn = 0;
// Mermaid's configuration is global and read again during a draw, so draws take turns.
let previous: Promise<unknown> = Promise.resolve();

/**
 * The diagram as a data: URL, and its accessible title and description if the
 * source gives them. Its colors are the design tokens where it appears, as
 * colors: an image cannot read the page's CSS variables.
 */
export function drawDiagram(source: string, where: HTMLElement): Promise<{ src: string; alt: string }> {
  const turn = previous.then(() => draw(source, where));
  previous = turn.catch(() => undefined);
  return turn;
}

async function draw(source: string, where: HTMLElement): Promise<{ src: string; alt: string }> {
  const c = tokenColors(where);
  // Series that must tell apart (pie slices, mindmap and timeline branches).
  const series = (["--color-brand", "--color-info", "--color-warning", "--color-danger", "--color-success", "--color-text-muted"] as const).map((token) => c[token]);
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    // SVG text labels: HTML labels are foreignObject, which an image may not draw.
    htmlLabels: false,
    theme: "base",
    // The tokens are already the color scheme's; Mermaid's darkMode would darken them again.
    themeVariables: {
      background: c["--color-bg-panel"],
      primaryColor: c["--color-brand-bg-subtle"],
      primaryBorderColor: c["--color-brand"],
      primaryTextColor: c["--color-text-main"],
      secondaryColor: c["--color-info-bg"],
      tertiaryColor: c["--color-bg-panel-subtle"],
      lineColor: c["--color-text-muted"],
      textColor: c["--color-text-main"],
      ...Object.fromEntries(series.flatMap((color, i) => [[`pie${i + 1}`, color], [`cScale${i}`, color]])),
      scaleLabelColor: "#ffffff",
      pieStrokeColor: c["--color-bg-panel"],
      pieSectionTextColor: "#ffffff",
      pieTitleTextColor: c["--color-text-main"],
      pieLegendTextColor: c["--color-text-main"],
    },
    // A Gantt chart spans the width of where it is drawn, which is off screen.
    gantt: { useWidth: Math.max(where.clientWidth, GANTT_WIDTH) },
  });
  const id = `branvil-diagram-${++drawn}`;
  let svg;
  try {
    ({ svg } = await mermaid.render(id, source));
  } finally {
    // A failed render leaves its drawing area at the end of the page.
    document.getElementById(`d${id}`)?.remove();
  }
  return svgImage(svg);
}

/** Mermaid's SVG as an image. Mermaid writes it as HTML (`&nbsp;`, …), which an XML parser refuses. */
export function svgImage(svg: string): { src: string; alt: string } {
  const root = new DOMParser().parseFromString(svg, "text/html").querySelector("svg")!;
  // An image needs its own size: Mermaid's SVG fills its container.
  const [, , width, height] = (root.getAttribute("viewBox") ?? "").split(/[\s,]+/).map(Number);
  if (width && height) {
    root.setAttribute("width", String(width));
    root.setAttribute("height", String(height));
    root.removeAttribute("style");
  }
  const alt = [root.querySelector(":scope > title"), root.querySelector(":scope > desc")]
    .map((e) => e?.textContent?.trim())
    .filter(Boolean)
    .join(": ");
  return { src: `data:image/svg+xml;charset=utf-8,${encodeURIComponent(new XMLSerializer().serializeToString(root))}`, alt };
}

/** The tokens as `#rrggbb`, which Mermaid can blend: a token may be light-dark() of oklch(). */
function tokenColors(where: HTMLElement): Record<(typeof TOKENS)[number], string> {
  const probe = where.appendChild(document.createElement("span"));
  const pixel = document.createElement("canvas").getContext("2d", { willReadFrequently: true })!;
  const colors = Object.fromEntries(
    TOKENS.map((token) => {
      probe.style.color = `var(${token})`;
      pixel.clearRect(0, 0, 1, 1);
      pixel.fillStyle = getComputedStyle(probe).color;
      pixel.fillRect(0, 0, 1, 1);
      const [r, g, b] = pixel.getImageData(0, 0, 1, 1).data;
      return [token, `#${[r, g, b].map((v) => v!.toString(16).padStart(2, "0")).join("")}`];
    }),
  ) as Record<(typeof TOKENS)[number], string>;
  probe.remove();
  return colors;
}
