import process from "node:process";

import { chromium } from "@playwright/test";

function parseArgs(argv) {
  const options = {
    baseUrl: "http://127.0.0.1:5173",
    primaryTitle: "",
    secondaryTitle: "",
    runs: 10,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const next = argv[index + 1];
    if (arg === "--base-url" && next) {
      options.baseUrl = next;
      index += 1;
    } else if (arg === "--primary-title" && next) {
      options.primaryTitle = next;
      index += 1;
    } else if (arg === "--secondary-title" && next) {
      options.secondaryTitle = next;
      index += 1;
    } else if (arg === "--runs" && next) {
      options.runs = Number.parseInt(next, 10);
      index += 1;
    }
  }
  if (!options.primaryTitle || !options.secondaryTitle) {
    throw new Error("Missing required --primary-title/--secondary-title");
  }
  return options;
}

function p95(values) {
  if (values.length === 1) {
    return values[0];
  }
  const sorted = [...values].sort((left, right) => left - right);
  const rank = Math.max(0, Math.ceil(sorted.length * 0.95) - 1);
  return sorted[rank];
}

async function selectConversation(page, title) {
  const button = page.getByRole("button", { name: new RegExp(title) }).first();
  const started = performance.now();
  await button.click();
  await page.getByRole("heading", { name: title }).waitFor({ state: "visible" });
  await page.getByRole("log").waitFor({ state: "visible" });
  return performance.now() - started;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const durations = [];
  try {
    await page.goto(options.baseUrl, { waitUntil: "networkidle" });
    await page.getByRole("tablist", { name: "Inspector Tabs" }).waitFor({ state: "visible" });
    await selectConversation(page, options.secondaryTitle);
    for (let index = 0; index < options.runs; index += 1) {
      const duration = await selectConversation(page, options.primaryTitle);
      durations.push(duration);
      await selectConversation(page, options.secondaryTitle);
    }
  } finally {
    await browser.close();
  }
  const average = durations.reduce((total, value) => total + value, 0) / durations.length;
  process.stdout.write(
    JSON.stringify(
      {
        sample: "ui_sidebar_select",
        runs: durations.length,
        durations_ms: durations.map((value) => Number(value.toFixed(2))),
        avg_ms: Number(average.toFixed(2)),
        p95_ms: Number(p95(durations).toFixed(2)),
        viewport: "1440x900",
        threshold_ms: 100,
      },
      null,
      2,
    ),
  );
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exit(1);
});
