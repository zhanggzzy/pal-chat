import type {
  AnalysisExportJob,
  AgentRun,
  AutomaticMetrics,
  BootstrapResponse,
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
  bootstrap: BootstrapResponse;
  conversations: ConversationRead[];
  detail: ConversationRead;
  runtimeAuthority: RuntimeAuthority | null;
  messages: Message[];
  cpRevisions: CpRevision[];
  runs: AgentRun[];
  logs: LogEntry[];
  episodes: CausalEpisode[];
  costs: CostBreakdown;
  memoryByAgent: Record<string, MemoryRevision[]>;
  contextByAgent: Record<string, ContextBundle[]>;
  attemptsByAgent: Record<string, RunAttempt[]>;
  automaticMetrics: AutomaticMetrics | null;
  manualScore: ManualScore | null;
  history: HistoryEntry[];
}

export async function loadWorkbench(
  apiBase: string,
  conversationId: string,
): Promise<WorkbenchPayload> {
  const bootstrap = await requestJson<BootstrapResponse>(apiBase, "/api/v1/bootstrap");
  const conversationList = await requestJson<{ items: ConversationRead[] }>(
    apiBase,
    "/api/v1/conversations",
  );
  const detailPayload = await requestJson<{
    conversation: ConversationRead;
    runtime_authority?: RuntimeAuthority | null;
  }>(
    apiBase,
    `/api/v1/conversations/${conversationId}`,
  );
  const detail = detailPayload.conversation;
  const hasArchive = detail.status !== "draft";
  const agentIds = [
    detail.locked_profile?.agent_a.agent_id ?? detail.draft_profile.agent_a.agent_id,
    detail.locked_profile?.agent_b.agent_id ?? detail.draft_profile.agent_b.agent_id,
  ];

  const [
    messages,
    cpRevisions,
    runs,
    logs,
    episodes,
    costs,
    memoryByAgent,
    contextByAgent,
    attemptsByAgent,
    automaticMetrics,
    manualScore,
    history,
  ] =
    await Promise.all([
      hasArchive
        ? requestJson<{ items: Message[] }>(apiBase, `/api/v1/conversations/${conversationId}/messages`)
        : Promise.resolve({ items: [] }),
      hasArchive
        ? requestJson<{ items: CpRevision[] }>(apiBase, `/api/v1/conversations/${conversationId}/cp-revisions`)
        : Promise.resolve({ items: [] }),
      hasArchive
        ? requestJson<{ items: AgentRun[] }>(apiBase, `/api/v1/conversations/${conversationId}/runs`)
        : Promise.resolve({ items: [] }),
      hasArchive
        ? requestJson<{ items: LogEntry[] }>(apiBase, `/api/v1/conversations/${conversationId}/logs`)
        : Promise.resolve({ items: [] }),
      hasArchive
        ? requestJson<{ items: CausalEpisode[] }>(
            apiBase,
            `/api/v1/conversations/${conversationId}/causal-episodes`,
          )
        : Promise.resolve({ items: [] }),
      hasArchive
        ? requestJson<CostBreakdown>(
            apiBase,
            `/api/v1/conversations/${conversationId}/metrics/cost-breakdown`,
          )
        : Promise.resolve({ total_tokens: 0, total_cost_usd: 0, by_agent: {}, by_phase: {} }),
      hasArchive
        ? Promise.all(
            agentIds.map(async (agentId) => [
              agentId,
              (
                await requestJson<{ items: MemoryRevision[] }>(
                  apiBase,
                  `/api/v1/conversations/${conversationId}/agents/${agentId}/memory/revisions`,
                )
              ).items,
            ]),
          ).then((entries) => Object.fromEntries(entries))
        : Promise.resolve({}),
      hasArchive
        ? Promise.all(
            agentIds.map(async (agentId) => [
              agentId,
              (
                await requestJson<{ items: ContextBundle[] }>(
                  apiBase,
                  `/api/v1/conversations/${conversationId}/agents/${agentId}/context-bundles`,
                )
              ).items,
            ]),
          ).then((entries) => Object.fromEntries(entries))
        : Promise.resolve({}),
      hasArchive
        ? Promise.all(
            agentIds.map(async (agentId) => [
              agentId,
              (
                await requestJson<{ items: RunAttempt[] }>(
                  apiBase,
                  `/api/v1/conversations/${conversationId}/agents/${agentId}/attempts`,
                )
              ).items,
            ]),
          ).then((entries) => Object.fromEntries(entries))
        : Promise.resolve({}),
      hasArchive
        ? requestJson<AutomaticMetrics>(
            apiBase,
            `/api/v1/conversations/${conversationId}/metrics/automatic`,
          )
        : Promise.resolve(null),
      hasArchive
        ? requestJson<ManualScore>(
            apiBase,
            `/api/v1/conversations/${conversationId}/manual-score`,
          )
        : Promise.resolve(null),
      requestJson<{ items: HistoryEntry[] }>(apiBase, "/api/v1/history").then((payload) => payload.items),
    ]);

  return {
    bootstrap,
    conversations: conversationList.items,
    detail,
    runtimeAuthority: detailPayload.runtime_authority ?? null,
    messages: messages.items,
    cpRevisions: cpRevisions.items,
    runs: runs.items,
    logs: logs.items,
    episodes: episodes.items,
    costs,
    memoryByAgent,
    contextByAgent,
    attemptsByAgent,
    automaticMetrics,
    manualScore,
    history,
  };
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
