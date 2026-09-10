/**
 * Accessibility and reflow audit over the pages scripts/render_pages.py wrote.
 *
 *   node scripts/audit_ui.mjs <directory>
 *
 * Two checks, both of which caught real defects when they were first run by
 * hand: axe-core at WCAG 2.2 A/AA, and horizontal overflow at the three widths
 * WCAG 1.4.10 cares about. Every page in this app overflowed at 320px until a
 * missing `min-width: 0` was found, and six pages carried controls a screen
 * reader announced with no name at all.
 *
 * This lives in the repo rather than calling a sibling toolkit so CI can run
 * it. It needs playwright and axe-core; the CI job installs both.
 */

import { readdirSync, readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import path from "node:path";
import { chromium } from "playwright";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const AXE_SOURCE = readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");

const WIDTHS = [280, 320, 414];

/**
 * The one violation this UI claims knowingly. The activity heatmap draws a
 * year as 10px cells; 24px targets would make a year view impossible, and
 * WCAG 2.2 allows an equivalent control instead — the date input beside it
 * drives the same filter and is keyboard reachable. See docs/DESIGN.md.
 *
 * Scoped as tightly as it can be: this rule, on these elements, on this page.
 * Anything else reporting target-size is a real finding.
 */
const KNOWN_EXCEPTIONS = [
  {
    page: "library",
    ruleId: "target-size",
    selectorPattern: /heatmap-day|data-date=/,
    why: "heatmap cells; equivalent date input provided (docs/DESIGN.md)",
  },
];

function isKnown(page, ruleId, nodes) {
  return KNOWN_EXCEPTIONS.some(
    (e) =>
      e.page === page &&
      e.ruleId === ruleId &&
      nodes.every((n) => e.selectorPattern.test(String(n.target)))
  );
}

async function auditPage(browser, file) {
  const name = path.basename(file, ".html");
  const url = pathToFileURL(file).href;
  const findings = [];

  const page = await browser.newPage();
  await page.goto(url, { waitUntil: "load" });

  // Accessibility, at the desktop width the app is mostly used at.
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.evaluate(AXE_SOURCE);
  const results = await page.evaluate(async () =>
    // eslint-disable-next-line no-undef
    await axe.run(document, { runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag22aa"] } })
  );

  for (const v of results.violations) {
    if (isKnown(name, v.id, v.nodes)) {
      console.log(`  ~ ${name}: ${v.id} (${v.nodes.length}) — known exception, allowed`);
      continue;
    }
    findings.push(
      `${name}: ${v.impact ?? "unknown"} ${v.id} — ${v.help} (${v.nodes.length} node(s))\n` +
        v.nodes
          .slice(0, 3)
          .map((n) => `      ${n.target}`)
          .join("\n")
    );
  }

  // Reflow. A page that scrolls sideways on a phone fails WCAG 1.4.10.
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 900 });
    const overflow = await page.evaluate(() => {
      const doc = document.documentElement;
      return doc.scrollWidth - doc.clientWidth;
    });
    if (overflow > 1) {
      const widest = await page.evaluate((vw) => {
        let worst = null;
        for (const el of document.querySelectorAll("*")) {
          const r = el.getBoundingClientRect();
          if (r.right > vw + 1 && (!worst || r.right > worst.right)) {
            worst = { right: r.right, tag: el.tagName.toLowerCase(), cls: String(el.className).slice(0, 40) };
          }
        }
        return worst;
      }, width);
      findings.push(
        `${name}: horizontal overflow of ${overflow}px at ${width}px` +
          (widest ? ` (widest: ${widest.tag}.${widest.cls})` : "")
      );
    }
  }

  await page.close();
  return findings;
}

async function main() {
  const dir = process.argv[2];
  if (!dir) {
    console.error("usage: node scripts/audit_ui.mjs <directory-of-rendered-pages>");
    return 2;
  }

  const files = readdirSync(dir)
    .filter((f) => f.endsWith(".html"))
    .map((f) => path.resolve(dir, f));

  if (files.length === 0) {
    console.error(`no .html files in ${dir} — did render_pages.py run?`);
    return 2;
  }

  const browser = await chromium.launch();
  const all = [];
  for (const file of files) {
    all.push(...(await auditPage(browser, file)));
  }
  await browser.close();

  console.log("");
  if (all.length > 0) {
    console.log(`FAIL — ${all.length} finding(s) across ${files.length} page(s):\n`);
    for (const f of all) console.log(`  x ${f}`);
    console.log("");
    console.log("docs/DESIGN.md explains the rules and the one allowed exception.");
    return 1;
  }

  console.log(`OK — ${files.length} page(s): no WCAG 2.2 A/AA violations, no reflow at 280/320/414px.`);
  return 0;
}

process.exit(await main());
