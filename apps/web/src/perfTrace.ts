import type { ProfilerOnRenderCallback } from "react";

interface PerfRequestTrace {
  id: string;
  path: string;
  started_at_ms: number;
  completed_at_ms: number | null;
  status: number | null;
}

interface PerfCommitTrace {
  id: string;
  phase: "mount" | "update" | "nested-update";
  actual_duration_ms: number;
  start_time_ms: number;
  commit_time_ms: number;
}

interface PerfBarrierTrace {
  label: string;
  at_ms: number;
}

interface PerfInteractionTrace {
  id: string;
  kind: string;
  metadata: Record<string, unknown>;
  started_at_ms: number;
  finished_at_ms: number | null;
  painted_at_ms: number | null;
  requests: PerfRequestTrace[];
  commits: PerfCommitTrace[];
  barriers: PerfBarrierTrace[];
}

interface PerfTraceStore {
  activeInteractionId: string | null;
  nextId: number;
  interactions: PerfInteractionTrace[];
  completed: PerfInteractionTrace[];
}

declare global {
  interface Window {
    __PAL_PERF_TRACE__?: PerfTraceStore;
  }
}

function nowMs(): number {
  return performance.timeOrigin + performance.now();
}

function createStore(): PerfTraceStore {
  return {
    activeInteractionId: null,
    nextId: 1,
    interactions: [],
    completed: [],
  };
}

function getStore(): PerfTraceStore | null {
  if (typeof window === "undefined") {
    return null;
  }
  window.__PAL_PERF_TRACE__ ??= createStore();
  return window.__PAL_PERF_TRACE__;
}

function nextId(prefix: string, store: PerfTraceStore): string {
  const value = `${prefix}-${store.nextId}`;
  store.nextId += 1;
  return value;
}

function getInteractionById(
  store: PerfTraceStore,
  interactionId: string,
): PerfInteractionTrace | null {
  return store.interactions.find((interaction) => interaction.id === interactionId) ?? null;
}

function getActiveInteraction(store: PerfTraceStore): PerfInteractionTrace | null {
  if (store.activeInteractionId === null) {
    return null;
  }
  return getInteractionById(store, store.activeInteractionId);
}

export function beginUiInteraction(
  kind: string,
  metadata: Record<string, unknown>,
): string | null {
  const store = getStore();
  if (!store) {
    return null;
  }
  const interaction: PerfInteractionTrace = {
    id: nextId("interaction", store),
    kind,
    metadata,
    started_at_ms: nowMs(),
    finished_at_ms: null,
    painted_at_ms: null,
    requests: [],
    commits: [],
    barriers: [],
  };
  store.interactions.push(interaction);
  store.activeInteractionId = interaction.id;
  return interaction.id;
}

export function recordWorkbenchRequestStart(path: string): string | null {
  const store = getStore();
  if (!store) {
    return null;
  }
  const interaction = getActiveInteraction(store);
  if (!interaction) {
    return null;
  }
  const requestId = nextId("request", store);
  interaction.requests.push({
    id: requestId,
    path,
    started_at_ms: nowMs(),
    completed_at_ms: null,
    status: null,
  });
  return requestId;
}

export function recordWorkbenchRequestEnd(
  requestId: string | null,
  status: number,
): void {
  const store = getStore();
  if (!store || requestId === null) {
    return;
  }
  for (const interaction of store.interactions) {
    const request = interaction.requests.find((entry) => entry.id === requestId);
    if (!request) {
      continue;
    }
    request.completed_at_ms = nowMs();
    request.status = status;
    return;
  }
}

export const recordReactProfilerCommit: ProfilerOnRenderCallback = (
  _id,
  phase,
  actualDuration,
  _baseDuration,
  startTime,
  commitTime,
) => {
  const store = getStore();
  if (!store) {
    return;
  }
  const interaction = getActiveInteraction(store);
  if (!interaction) {
    return;
  }
  interaction.commits.push({
    id: nextId("commit", store),
    phase,
    actual_duration_ms: Number(actualDuration.toFixed(3)),
    start_time_ms: performance.timeOrigin + startTime,
    commit_time_ms: performance.timeOrigin + commitTime,
  });
};

export async function markInteractionPainted(
  interactionId: string | null,
  label: string,
): Promise<void> {
  const store = getStore();
  if (!store || interactionId === null) {
    return;
  }
  const interaction = getInteractionById(store, interactionId);
  if (!interaction || interaction.finished_at_ms !== null) {
    return;
  }
  await new Promise<void>((resolve) => {
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        resolve();
      });
    });
  });
  const timestamp = nowMs();
  interaction.barriers.push({ label, at_ms: timestamp });
  interaction.painted_at_ms = timestamp;
  interaction.finished_at_ms = timestamp;
  if (store.activeInteractionId === interaction.id) {
    store.activeInteractionId = null;
  }
  store.completed.push(structuredClone(interaction));
}

export function consumeCompletedUiInteractions(): PerfInteractionTrace[] {
  const store = getStore();
  if (!store) {
    return [];
  }
  const completed = structuredClone(store.completed);
  store.completed = [];
  return completed;
}

export function resetUiPerfTrace(): void {
  const store = getStore();
  if (!store) {
    return;
  }
  store.activeInteractionId = null;
  store.interactions = [];
  store.completed = [];
}
