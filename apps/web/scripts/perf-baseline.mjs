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

function metricMap(metrics) {
  return Object.fromEntries(metrics.map((entry) => [entry.name, entry.value]));
}

function round(value) {
  return Number(value.toFixed(2));
}

function extractCommitEnvelope(commits) {
  if (!commits.length) {
    return {
      firstStartMs: null,
      lastCommitMs: null,
      actualDurationMs: 0,
      windowMs: 0,
    };
  }
  const firstStartMs = Math.min(...commits.map((commit) => commit.start_time_ms));
  const lastCommitMs = Math.max(...commits.map((commit) => commit.commit_time_ms));
  const actualDurationMs = commits.reduce((total, commit) => total + commit.actual_duration_ms, 0);
  return {
    firstStartMs,
    lastCommitMs,
    actualDurationMs,
    windowMs: Math.max(0, lastCommitMs - firstStartMs),
  };
}

function extractRequestEnvelope(requests) {
  if (!requests.length) {
    return {
      firstStartMs: null,
      lastEndMs: null,
      durationMs: 0,
    };
  }
  const started = Math.min(...requests.map((request) => request.started_at_ms));
  const completed = Math.max(
    ...requests.map((request) => request.completed_at_ms ?? request.started_at_ms),
  );
  return {
    firstStartMs: started,
    lastEndMs: completed,
    durationMs: Math.max(0, completed - started),
  };
}

function buildTimeline(trace) {
  const request = extractRequestEnvelope(trace.requests);
  const commits = extractCommitEnvelope(trace.commits);
  const paintedAtMs = trace.painted_at_ms ?? trace.finished_at_ms ?? trace.started_at_ms;
  const preRequestGapMs =
    request.firstStartMs === null ? 0 : Math.max(0, request.firstStartMs - trace.started_at_ms);
  const responseToCommitGapMs =
    request.lastEndMs === null || commits.firstStartMs === null
      ? 0
      : Math.max(0, commits.firstStartMs - request.lastEndMs);
  const commitToPaintGapMs =
    commits.lastCommitMs === null ? 0 : Math.max(0, paintedAtMs - commits.lastCommitMs);
  const accountedMs =
    preRequestGapMs +
    request.durationMs +
    responseToCommitGapMs +
    commits.windowMs +
    commitToPaintGapMs;
  return {
    pre_request_gap_ms: round(preRequestGapMs),
    request_ms: round(request.durationMs),
    response_to_commit_gap_ms: round(responseToCommitGapMs),
    react_commit_window_ms: round(commits.windowMs),
    react_commit_actual_ms: round(commits.actualDurationMs),
    commit_to_painted_gap_ms: round(commitToPaintGapMs),
    scheduler_gap_ms: round(Math.max(0, paintedAtMs - trace.started_at_ms - accountedMs)),
  };
}

async function readCdpMetrics(client) {
  const response = await client.send("Performance.getMetrics");
  return metricMap(response.metrics ?? []);
}

function browserMetricDelta(before, after) {
  const toMilliseconds = (name) => round(((after[name] ?? 0) - (before[name] ?? 0)) * 1000);
  const toCount = (name) => (after[name] ?? 0) - (before[name] ?? 0);
  return {
    layout_duration_ms: toMilliseconds("LayoutDuration"),
    recalc_style_duration_ms: toMilliseconds("RecalcStyleDuration"),
    script_duration_ms: toMilliseconds("ScriptDuration"),
    task_duration_ms: toMilliseconds("TaskDuration"),
    layout_count: toCount("LayoutCount"),
    recalc_style_count: toCount("RecalcStyleCount"),
  };
}

async function installBrowserPerfHooks(page) {
  await page.addInitScript(() => {
    window.__PAL_BROWSER_PERF__ = {
      current: null,
      completed: [],
      start(interactionId) {
        this.current = { interactionId, longtasks: [], startedAtMs: performance.now(), endedAtMs: null };
      },
      finish() {
        if (!this.current) {
          return;
        }
        this.current.endedAtMs = performance.now();
        this.completed.push(structuredClone(this.current));
        this.current = null;
      },
      consume() {
        const completed = structuredClone(this.completed);
        this.completed = [];
        return completed;
      },
      reset() {
        this.current = null;
        this.completed = [];
      },
    };
    const supported = PerformanceObserver.supportedEntryTypes ?? [];
    if (supported.includes("longtask")) {
      const observer = new PerformanceObserver((entries) => {
        const perf = window.__PAL_BROWSER_PERF__;
        if (!perf?.current) {
          return;
        }
        for (const entry of entries.getEntries()) {
          perf.current.longtasks.push({
            name: entry.name,
            start_time_ms: entry.startTime,
            duration_ms: entry.duration,
          });
        }
      });
      observer.observe({ type: "longtask", buffered: true });
    }
  });
}

async function drainCompletedUiTrace(page, timeoutMs = 5000) {
  await page.waitForFunction(() => (window.__PAL_PERF_TRACE__?.completed?.length ?? 0) > 0, null, {
    timeout: timeoutMs,
  });
  const completed = await page.evaluate(() => {
    const store = window.__PAL_PERF_TRACE__;
    const traces = structuredClone(store?.completed ?? []);
    if (store) {
      store.completed = [];
    }
    return traces;
  });
  return completed.at(-1) ?? null;
}

async function selectConversation(page, client, title) {
  const button = page.getByRole("button", { name: new RegExp(title) }).first();
  await page.evaluate(() => {
    if (window.__PAL_PERF_TRACE__) {
      window.__PAL_PERF_TRACE__.completed = [];
    }
    window.__PAL_BROWSER_PERF__?.reset();
  });
  const beforeMetrics = await readCdpMetrics(client);
  const wallStarted = performance.now();
  await button.click();
  const trace = await drainCompletedUiTrace(page);
  if (!trace) {
    throw new Error(`Missing completed UI perf trace for ${title}`);
  }
  await page.evaluate(() => {
    window.__PAL_BROWSER_PERF__?.finish();
  });
  await page.getByRole("heading", { name: title }).waitFor({ state: "visible" });
  await page.getByRole("log").waitFor({ state: "visible" });
  const afterMetrics = await readCdpMetrics(client);
  const browserPerf = await page.evaluate(() => window.__PAL_BROWSER_PERF__?.consume?.() ?? []);
  const totalDurationMs = performance.now() - wallStarted;
  const timeline = buildTimeline(trace);
  const longtasks = browserPerf.at(-1)?.longtasks ?? [];
  const longtaskTotalMs = longtasks.reduce((total, entry) => total + entry.duration_ms, 0);
  const browserMetrics = browserMetricDelta(beforeMetrics, afterMetrics);
  return {
    duration_ms: round(totalDurationMs),
    trace,
    browser_metrics: {
      ...browserMetrics,
      longtask_total_ms: round(longtaskTotalMs),
      longtask_count: longtasks.length,
      painted_barrier_ms: round(
        Math.max(
          0,
          (trace.painted_at_ms ?? trace.finished_at_ms ?? trace.started_at_ms) - trace.started_at_ms,
        ),
      ),
    },
    longtasks: longtasks.map((entry) => ({
      name: entry.name,
      start_time_ms: round(entry.start_time_ms),
      duration_ms: round(entry.duration_ms),
    })),
    timeline,
  };
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const client = await page.context().newCDPSession(page);
  await client.send("Performance.enable");
  await installBrowserPerfHooks(page);
  const durations = [];
  const rawSamples = [];
  try {
    const url = new URL(options.baseUrl);
    url.searchParams.set("pal_perf_trace", "1");
    await page.goto(url.toString(), { waitUntil: "networkidle" });
    await page.getByRole("tablist", { name: "Inspector Tabs" }).waitFor({ state: "visible" });
    await selectConversation(page, client, options.secondaryTitle);
    for (let index = 0; index < options.runs; index += 1) {
      const sample = await selectConversation(page, client, options.primaryTitle);
      durations.push(sample.duration_ms);
      rawSamples.push({
        run: index + 1,
        kind: index === 0 ? "cold" : "warm",
        duration_ms: sample.duration_ms,
        target_title: options.primaryTitle,
        timeline: sample.timeline,
        browser_metrics: sample.browser_metrics,
        longtasks: sample.longtasks,
        trace: sample.trace,
      });
      await selectConversation(page, client, options.secondaryTitle);
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
        durations_ms: durations.map((value) => round(value)),
        raw_samples: rawSamples,
        avg_ms: round(average),
        p95_ms: round(p95(durations)),
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
