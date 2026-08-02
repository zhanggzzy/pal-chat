import type {
  AnalysisExportJob,
  AgentRun,
  AutomaticMetrics,
  BootstrapResponse,
  CausalEpisode,
  ContextBundle,
  ConversationDetail,
  ConversationRead,
  CostBreakdown,
  CpRevision,
  CredentialRead,
  ExperimentProfile,
  HistoryDetail,
  HistoryEntry,
  LogEntry,
  ManualScore,
  MemoryRevision,
  Message,
  ProfileTemplateRead,
  RuntimeAuthority,
  RunAttempt,
  RuntimeGuardrails,
  ValidationResult,
} from "./types";
import { recordWorkbenchRequestEnd, recordWorkbenchRequestStart } from "./perfTrace";

export async function requestJson<T>(
  apiBase: string,
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const shouldTraceWorkbench = path.includes("/workbench");
  const requestId = shouldTraceWorkbench ? recordWorkbenchRequestStart(path) : null;
  const response = await fetch(`${apiBase.replace(/\/$/, "")}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers ?? {}),
    },
    ...options,
  });
  if (shouldTraceWorkbench) {
    recordWorkbenchRequestEnd(requestId, response.status);
  }
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

export async function loadBootstrap(apiBase: string): Promise<BootstrapResponse> {
  return requestJson<BootstrapResponse>(apiBase, "/api/v1/bootstrap");
}

export async function listConversations(apiBase: string): Promise<ConversationRead[]> {
  return requestJson<{ items: ConversationRead[] }>(apiBase, "/api/v1/conversations").then(
    (payload) => payload.items,
  );
}

export async function listHistory(apiBase: string): Promise<HistoryEntry[]> {
  return requestJson<{ items: HistoryEntry[] }>(apiBase, "/api/v1/history").then(
    (payload) => payload.items,
  );
}

export async function listTemplates(apiBase: string): Promise<ProfileTemplateRead[]> {
  return requestJson<{ items: ProfileTemplateRead[] }>(apiBase, "/api/v1/profile-templates").then(
    (payload) => payload.items,
  );
}

export async function listCredentials(apiBase: string): Promise<CredentialRead[]> {
  return requestJson<{ items: CredentialRead[] }>(apiBase, "/api/v1/credentials").then(
    (payload) => payload.items,
  );
}

export async function createCredential(
  apiBase: string,
  payload: { provider: string; label: string; secret: string },
): Promise<CredentialRead> {
  return requestJson<{ credential: CredentialRead }>(apiBase, "/api/v1/credentials", {
    method: "POST",
    body: JSON.stringify(payload),
  }).then((data) => data.credential);
}

export async function validateCredential(
  apiBase: string,
  credentialRef: string,
): Promise<CredentialRead> {
  return requestJson<{ credential: CredentialRead }>(
    apiBase,
    `/api/v1/credentials/${credentialRef}/validate`,
    { method: "POST" },
  ).then((data) => data.credential);
}

export async function deleteCredential(apiBase: string, credentialRef: string): Promise<void> {
  await requestJson<null>(apiBase, `/api/v1/credentials/${credentialRef}`, { method: "DELETE" });
}

export async function cloneTemplateProfile(
  apiBase: string,
  templateId: string,
): Promise<ExperimentProfile> {
  return requestJson<{ profile: ExperimentProfile }>(
    apiBase,
    `/api/v1/profile-templates/${templateId}/clone`,
    { method: "POST" },
  ).then((payload) => payload.profile);
}

export async function createConversation(
  apiBase: string,
  payload: { title: string; profile_template_id?: string | null; draft_profile?: ExperimentProfile | null },
): Promise<ConversationRead> {
  return requestJson<ConversationRead>(apiBase, "/api/v1/conversations", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function loadConversationDetail(
  apiBase: string,
  conversationId: string,
): Promise<ConversationDetail> {
  return requestJson<ConversationDetail>(apiBase, `/api/v1/conversations/${conversationId}`);
}

export async function updateDraftProfile(
  apiBase: string,
  conversationId: string,
  draftProfile: ExperimentProfile,
): Promise<ConversationRead> {
  return requestJson<ConversationRead>(apiBase, `/api/v1/conversations/${conversationId}/draft-profile`, {
    method: "PUT",
    body: JSON.stringify({ draft_profile: draftProfile }),
  });
}

export async function validateConversation(
  apiBase: string,
  conversationId: string,
): Promise<ValidationResult> {
  return requestJson<ValidationResult>(apiBase, `/api/v1/conversations/${conversationId}/validate`, {
    method: "POST",
  });
}

export async function conversationAction(
  apiBase: string,
  conversationId: string,
  action: "start" | "pause" | "resume" | "end",
): Promise<ConversationRead> {
  return requestJson<ConversationRead>(apiBase, `/api/v1/conversations/${conversationId}/${action}`, {
    method: "POST",
  });
}

export async function cloneConversationProfile(
  apiBase: string,
  conversationId: string,
): Promise<ExperimentProfile> {
  return requestJson<{ profile: ExperimentProfile }>(
    apiBase,
    `/api/v1/conversations/${conversationId}/clone-profile`,
    { method: "POST" },
  ).then((payload) => payload.profile);
}

export async function updateConversationMetadata(
  apiBase: string,
  conversationId: string,
  metadata: Record<string, unknown>,
): Promise<ConversationRead> {
  return requestJson<ConversationRead>(apiBase, `/api/v1/conversations/${conversationId}/catalog-metadata`, {
    method: "PATCH",
    body: JSON.stringify({ metadata }),
  });
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

export async function submitMessage(
  apiBase: string,
  conversationId: string,
  payload: {
    client_message_id: string;
    content_markdown: string;
    mentions: string[];
    primary_reply_to: string | null;
    responds_to: string[];
  },
): Promise<Message> {
  return requestJson<{ message: Message }>(apiBase, `/api/v1/conversations/${conversationId}/messages`, {
    method: "POST",
    body: JSON.stringify(payload),
  }).then((response) => response.message);
}

export async function retryMessage(
  apiBase: string,
  conversationId: string,
  clientMessageId: string,
): Promise<Message> {
  return requestJson<{ message: Message }>(
    apiBase,
    `/api/v1/conversations/${conversationId}/submissions/${clientMessageId}/retry`,
    { method: "POST" },
  ).then((response) => response.message);
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

export async function updateGuardrails(
  apiBase: string,
  conversationId: string,
  payload: Partial<RuntimeGuardrails>,
): Promise<ConversationRead> {
  return requestJson<ConversationRead>(apiBase, `/api/v1/conversations/${conversationId}/guardrails`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
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
