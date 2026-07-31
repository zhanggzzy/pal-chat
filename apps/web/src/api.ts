import type {
  AnalysisExportJob,
  AgentRun,
  AutomaticMetrics,
  CausalEpisode,
  ContextBundle,
  ConversationRead,
  CostBreakdown,
  CpRevision,
  HistoryDetail,
  HistoryEntry,
  LogEntry,
  ManualScore,
  MemoryRevision,
  Message,
  RuntimeAuthority,
  RunAttempt,
} from "./types";

export async function requestJson<T>(
  apiBase: string,
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${apiBase.replace(/\/$/, "")}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers ?? {}),
    },
    ...options,
  });
  const text = await response.text();
  const data = text ? (JSON.parse(text) as unknown) : null;
  if (!response.ok) {
    const message =
      typeof data === "object" && data && "error" in data
        ? String((data as { error: { message?: string } }).error.message ?? response.statusText)
        : response.statusText;
    throw new Error(message);
  }
  return data as T;
}

export interface WorkbenchPayload {
  detail: ConversationRead;
  runtimeAuthority: RuntimeAuthority | null;
  messages: Message[];
}

export async function loadWorkbench(
  apiBase: string,
  conversationId: string,
): Promise<WorkbenchPayload> {
  const payload = await requestJson<{
    detail: {
      conversation: ConversationRead;
      runtime_authority?: RuntimeAuthority | null;
    };
    messages: Message[];
  }>(apiBase, `/api/v1/conversations/${conversationId}/workbench`);

  return {
    detail: payload.detail.conversation,
    runtimeAuthority: payload.detail.runtime_authority ?? null,
    messages: payload.messages,
  };
}

export interface ServerInspectorPayload {
  cpRevisions: CpRevision[];
  runs: AgentRun[];
  logs: LogEntry[];
  episodes: CausalEpisode[];
  costs: CostBreakdown;
  automaticMetrics: AutomaticMetrics | null;
  manualScore: ManualScore | null;
}

export async function loadServerInspector(
  apiBase: string,
  conversationId: string,
): Promise<ServerInspectorPayload> {
  const [
    cpRevisions,
    runs,
    logs,
    episodes,
    costs,
    automaticMetrics,
    manualScore,
  ] = await Promise.all([
    requestJson<{ items: CpRevision[] }>(
      apiBase,
      `/api/v1/conversations/${conversationId}/cp-revisions`,
    ).then((payload) => payload.items),
    requestJson<{ items: AgentRun[] }>(
      apiBase,
      `/api/v1/conversations/${conversationId}/runs`,
    ).then((payload) => payload.items),
    requestJson<{ items: LogEntry[] }>(
      apiBase,
      `/api/v1/conversations/${conversationId}/logs`,
    ).then((payload) => payload.items),
    requestJson<{ items: CausalEpisode[] }>(
      apiBase,
      `/api/v1/conversations/${conversationId}/causal-episodes`,
    ).then((payload) => payload.items),
    requestJson<CostBreakdown>(
      apiBase,
      `/api/v1/conversations/${conversationId}/metrics/cost-breakdown`,
    ),
    requestJson<AutomaticMetrics | null>(
      apiBase,
      `/api/v1/conversations/${conversationId}/metrics/automatic`,
    ),
    requestJson<ManualScore | null>(
      apiBase,
      `/api/v1/conversations/${conversationId}/manual-score`,
    ),
  ]);
  return {
    cpRevisions,
    runs,
    logs,
    episodes,
    costs,
    automaticMetrics,
    manualScore,
  };
}

export interface AgentInspectorPayload {
  memory: MemoryRevision[];
  contextBundles: ContextBundle[];
  attempts: RunAttempt[];
}

export async function loadAgentInspector(
  apiBase: string,
  conversationId: string,
  agentId: string,
): Promise<AgentInspectorPayload> {
  const [memory, contextBundles, attempts] = await Promise.all([
    requestJson<{ items: MemoryRevision[] }>(
      apiBase,
      `/api/v1/conversations/${conversationId}/agents/${agentId}/memory/revisions`,
    ).then((payload) => payload.items),
    requestJson<{ items: ContextBundle[] }>(
      apiBase,
      `/api/v1/conversations/${conversationId}/agents/${agentId}/context-bundles`,
    ).then((payload) => payload.items),
    requestJson<{ items: RunAttempt[] }>(
      apiBase,
      `/api/v1/conversations/${conversationId}/agents/${agentId}/attempts`,
    ).then((payload) => payload.items),
  ]);
  return { memory, contextBundles, attempts };
}

export async function loadHistoryDetail(
  apiBase: string,
  conversationId: string,
): Promise<HistoryDetail> {
  return requestJson<HistoryDetail>(apiBase, `/api/v1/history/${conversationId}`);
}

export async function saveManualScore(
  apiBase: string,
  conversationId: string,
  payload: Pick<ManualScore, "rubric_version" | "scores" | "overall_note">,
): Promise<ManualScore> {
  return requestJson<ManualScore>(apiBase, `/api/v1/conversations/${conversationId}/manual-score`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function createAnalysisExport(
  apiBase: string,
  conversationId: string,
): Promise<AnalysisExportJob> {
  return requestJson<AnalysisExportJob>(
    apiBase,
    `/api/v1/conversations/${conversationId}/analysis-exports`,
    { method: "POST" },
  );
}

export async function getAnalysisExport(
  apiBase: string,
  conversationId: string,
  jobId: string,
): Promise<AnalysisExportJob> {
  return requestJson<AnalysisExportJob>(
    apiBase,
    `/api/v1/conversations/${conversationId}/analysis-exports/${jobId}`,
  );
}
