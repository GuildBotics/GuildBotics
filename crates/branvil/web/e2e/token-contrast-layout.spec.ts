// Browser color resolution covers light-dark(), OKLCH and the sRGB gamut.
import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

const css = readFileSync(new URL("../../../../packages/ui/src/styles.css", import.meta.url), "utf8");
const tones = [...css.matchAll(/--color-([\w]+)-text:/g)].map((match) => match[1]);

for (const scheme of ["light", "dark"] as const) {
  test(`semantic status text remains readable in ${scheme}`, async ({ page }) => {
    expect(tones.length).toBeGreaterThan(0);
    await page.setContent(`<style>${css}</style><span id="probe">Status</span>`);
    const ratios = await page.evaluate(({ scheme, tones }) => {
      document.documentElement.style.colorScheme = scheme;
      const probe = document.getElementById("probe")!;
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 1;
      const context = canvas.getContext("2d", { willReadFrequently: true })!;
      function luminance(color: string) {
        context.fillStyle = color;
        context.fillRect(0, 0, 1, 1);
        const rgb = [...context.getImageData(0, 0, 1, 1).data].slice(0, 3).map((value) => {
          const channel = value / 255;
          return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
        });
        return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
      }
      return tones.map((tone) => {
        probe.style.color = `var(--color-${tone}-text)`;
        probe.style.backgroundColor = `var(--color-${tone}-bg)`;
        const style = getComputedStyle(probe);
        const foreground = luminance(style.color);
        const background = luminance(style.backgroundColor);
        return { tone, ratio: (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05) };
      });
    }, { scheme, tones });
    for (const { tone, ratio } of ratios) {
      console.log(`${scheme} ${tone}: ${ratio.toFixed(2)}:1`);
      expect(ratio, `${scheme} ${tone}`).toBeGreaterThanOrEqual(4.5);
    }
  });
}
