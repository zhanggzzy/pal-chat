import type {
  AgentRun,
  BootstrapResponse,
  CausalEpisode,
  ContextBundle,
  ConversationRead,
  CostBreakdown,
  CpRevision,
  LogEntry,
  MemoryRevision,
  Message,
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
  messages: Message[];
  cpRevisions: CpRevision[];
  runs: AgentRun[];
  logs: LogEntry[];
  episodes: CausalEpisode[];
  costs: CostBreakdown;
  memoryByAgent: Record<string, MemoryRevision[]>;
  contextByAgent: Record<string, ContextBundle[]>;
  attemptsByAgent: Record<string, RunAttempt[]>;
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
  const detailPayload = await requestJson<{ conversation: ConversationRead }>(
    apiBase,
    `/api/v1/conversations/${conversationId}`,
  );
  const detail = detailPayload.conversation;
  const agentIds = [
    detail.locked_profile?.agent_a.agent_id ?? detail.draft_profile.agent_a.agent_id,
    detail.locked_profile?.agent_b.agent_id ?? detail.draft_profile.agent_b.agent_id,
  ];

  const [messages, cpRevisions, runs, logs, episodes, costs, memoryByAgent, contextByAgent, attemptsByAgent] =
    await Promise.all([
      requestJson<{ items: Message[] }>(apiBase, `/api/v1/conversations/${conversationId}/messages`),
      requestJson<{ items: CpRevision[] }>(apiBase, `/api/v1/conversations/${conversationId}/cp-revisions`),
      requestJson<{ items: AgentRun[] }>(apiBase, `/api/v1/conversations/${conversationId}/runs`),
      requestJson<{ items: LogEntry[] }>(apiBase, `/api/v1/conversations/${conversationId}/logs`),
      requestJson<{ items: CausalEpisode[] }>(
        apiBase,
        `/api/v1/conversations/${conversationId}/causal-episodes`,
      ),
      requestJson<CostBreakdown>(
        apiBase,
        `/api/v1/conversations/${conversationId}/metrics/cost-breakdown`,
      ),
      Promise.all(
        agentIds.map(async (agentId) => [
          agentId,
          (
            await requestJson<{ items: MemoryRevision[] }>(
              apiBase,
              `/api/v1/conversations/${conversationId}/agents/${agentId}/memory/revisions`,
            )
          ).items,
        ]),
      ).then((entries) => Object.fromEntries(entries)),
      Promise.all(
        agentIds.map(async (agentId) => [
          agentId,
          (
            await requestJson<{ items: ContextBundle[] }>(
              apiBase,
              `/api/v1/conversations/${conversationId}/agents/${agentId}/context-bundles`,
            )
          ).items,
        ]),
      ).then((entries) => Object.fromEntries(entries)),
      Promise.all(
        agentIds.map(async (agentId) => [
          agentId,
          (
            await requestJson<{ items: RunAttempt[] }>(
              apiBase,
              `/api/v1/conversations/${conversationId}/agents/${agentId}/attempts`,
            )
          ).items,
        ]),
      ).then((entries) => Object.fromEntries(entries)),
    ]);

  return {
    bootstrap,
    conversations: conversationList.items,
    detail,
    messages: messages.items,
    cpRevisions: cpRevisions.items,
    runs: runs.items,
    logs: logs.items,
    episodes: episodes.items,
    costs,
    memoryByAgent,
    contextByAgent,
    attemptsByAgent,
  };
}
