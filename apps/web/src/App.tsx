import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import {
  cloneConversationProfile,
  cloneTemplateProfile,
  conversationAction,
  createAnalysisExport,
  createConversation,
  createCredential,
  deleteCredential,
  getAnalysisExport,
  listConversations,
  listCredentials,
  listHistory,
  loadAgentInspector,
  loadBootstrap,
  loadConversationDetail,
  loadHistoryDetail,
  loadServerInspector,
  loadWorkbench,
  retryMessage,
  saveManualScore,
  submitMessage,
  updateConversationMetadata,
  updateDraftProfile,
  updateGuardrails,
  validateConversation,
  validateCredential,
} from "./api";
import { beginUiInteraction, markInteractionPainted } from "./perfTrace";
import { applySocketPayload, emptyRuntimeViewState, inferRunForMessage, upsertMessage } from "./state";
import "./styles.css";
import type {
  AgentProfile,
  AgentRun,
  AnalysisExportJob,
  AutomaticMetrics,
  BootstrapResponse,
  ContextBundle,
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
  OutboxEvent,
  ReviewState,
  RuntimeAuthority,
  RuntimeGuardrails,
  SessionSnapshot,
  ValidationResult,
} from "./types";

type InspectorView = "agent-a" | "agent-b" | "server";

type MentionOption = {
  token: string;
  values: string[];
  description: string;
};

type MentionState = {
  start: number;
  end: number;
  query: string;
};

type WizardStepId =
  | "template"
  | "roles"
  | "models"
  | "attention"
  | "params"
  | "projection"
  | "guardrails"
  | "validation"
  | "confirm";

type WizardStep = {
  id: WizardStepId;
  title: string;
  description: string;
};

type DragState =
  | { type: "main"; startX: number; initialRatio: number }
  | { type: "secondary"; startY: number; initialRatio: number }
  | null;

const WIZARD_STEPS: WizardStep[] = [
  { id: "template", title: "1. 模板与策略包", description: "选择模板、命名实验并确认基础策略包。" },
  { id: "roles", title: "2. Agent 角色", description: "配置 Agent A/B 的显示名与 persona。" },
  { id: "models", title: "3. 模型与凭据", description: "为两个 Agent 选择模型、Provider 和凭据。" },
  { id: "attention", title: "4. Attention Prior", description: "设置关注倾向与响应阈值。" },
  { id: "params", title: "5. 关键参数", description: "调整 prompt 版本与关键运行参数。" },
  { id: "projection", title: "6. Projection 模型", description: "选择投影与消息序列模块。" },
  { id: "guardrails", title: "7. 初始护栏", description: "设置初始 Guardrails 和日志级别。" },
  { id: "validation", title: "8. 兼容校验", description: "先做前端预检，再创建草稿后执行服务端校验。" },
  { id: "confirm", title: "9. 配置 Diff 与启动确认", description: "查看与模板差异，创建草稿并进入启动准备。" },
];

function formatTime(value: string | null | undefined): string {
  if (!value) {
    return "-";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function asJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function fallbackUrl(): string {
  const envApiBase = import.meta.env.VITE_API_BASE;
  if (typeof envApiBase === "string" && envApiBase.length > 0) {
    return envApiBase;
  }
  if (typeof window !== "undefined" && window.location.origin.startsWith("http")) {
    const url = new URL(window.location.origin);
    const localHosts = new Set(["localhost", "127.0.0.1"]);
    if (localHosts.has(url.hostname) && new Set(["4173", "5173"]).has(url.port)) {
      url.port = "8000";
      return url.toString().replace(/\/$/, "");
    }
    return window.location.origin;
  }
  return "http://127.0.0.1:8000";
}

function normalizeManualScore(payload: ManualScore | null): ManualScore | null {
  if (!payload) {
    return null;
  }
  if (payload.scores.length > 0) {
    return payload;
  }
  return {
    ...payload,
    scores: [
      { criterion: "clarity", score: null, note: "" },
      { criterion: "safety", score: null, note: "" },
      { criterion: "traceability", score: null, note: "" },
    ],
  };
}

function activeProfile(conversation: ConversationRead | null): ExperimentProfile | null {
  if (!conversation) {
    return null;
  }
  return conversation.locked_profile ?? conversation.draft_profile;
}

function agentProfiles(conversation: ConversationRead | null): [AgentProfile, AgentProfile] | null {
  const profile = activeProfile(conversation);
  if (!profile) {
    return null;
  }
  return [profile.agent_a, profile.agent_b];
}

function typingFromAuthority(
  authority: RuntimeAuthority | null,
): Record<string, { active: boolean; runId: string | null; reason?: string }> {
  if (!authority) {
    return {};
  }
  return Object.fromEntries(
    Object.entries(authority.agents).map(([agentId, state]) => [
      agentId,
      {
        active: state.typing_status === "active",
        runId: state.typing_run_id,
        reason: state.typing_status === "active" ? "authoritative" : "idle",
      },
    ]),
  );
}

function cloneProfile(profile: ExperimentProfile): ExperimentProfile {
  return JSON.parse(JSON.stringify(profile)) as ExperimentProfile;
}

function enableUiRuntime(profile: ExperimentProfile): ExperimentProfile {
  const metadata = { ...(profile.metadata ?? {}) };
  if (metadata.agent_runtime_enabled !== true) {
    metadata.agent_runtime_enabled = true;
  }
  const phaseValue = Number(metadata.phase ?? 1);
  if (!Number.isFinite(phaseValue) || phaseValue < 3) {
    metadata.phase = 3;
  }
  return {
    ...profile,
    metadata,
  };
}

function parseTags(value: string): string[] {
  return value
    .split(/[，,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function tagsText(value: unknown): string {
  if (Array.isArray(value)) {
    return value.join(", ");
  }
  return "";
}

function mentionCandidates(profiles: [AgentProfile, AgentProfile] | null): MentionOption[] {
  if (!profiles) {
    return [{ token: "@all", values: ["agent-a", "agent-b"], description: "让两位 Agent 都加入这轮对话" }];
  }
  return [
    {
      token: "@all",
      values: [profiles[0].agent_id, profiles[1].agent_id],
      description: "让两位 Agent 都加入这轮对话",
    },
    { token: "@A", values: [profiles[0].agent_id], description: profiles[0].display_name },
    { token: "@B", values: [profiles[1].agent_id], description: profiles[1].display_name },
  ];
}

function detectMention(text: string, cursor: number): MentionState | null {
  const prefix = text.slice(0, cursor);
  const match = prefix.match(/(^|\s)(@\S*)$/);
  if (!match) {
    return null;
  }
  const token = match[2];
  const start = cursor - token.length;
  return {
    start,
    end: cursor,
    query: token.slice(1).toLowerCase(),
  };
}

function collectMentions(text: string, options: MentionOption[]): string[] {
  const matches = text.match(/@\S+/g) ?? [];
  const values: string[] = [];
  for (const match of matches) {
    const option = options.find((item) => item.token.toLowerCase() === match.toLowerCase());
    if (!option) {
      continue;
    }
    for (const value of option.values) {
      if (!values.includes(value)) {
        values.push(value);
      }
    }
  }
  return values;
}

function messageSummary(message: Message | null): string {
  if (!message) {
    return "";
  }
  return message.content_markdown.length > 60
    ? `${message.content_markdown.slice(0, 60)}...`
    : message.content_markdown;
}

function messageDisplayName(message: Message, profiles: [AgentProfile, AgentProfile] | null): string {
  if (message.sender_kind === "user") {
    return "你";
  }
  if (!profiles) {
    return message.sender_id;
  }
  return (
    profiles.find((item) => item.agent_id === message.sender_id)?.display_name ?? message.sender_id
  );
}

function templateProfile(templates: BootstrapResponse["templates"], templateId: string): ExperimentProfile | null {
  const template = templates.find((item) => item.id === templateId);
  return template ? cloneProfile(template.profile) : null;
}

function summarizeProfileDiff(profile: ExperimentProfile, baseline: ExperimentProfile | null): string[] {
  if (!baseline) {
    return ["未找到模板基线，将按当前配置创建草稿。"];
  }
  const lines: string[] = [];
  if (profile.title !== baseline.title) {
    lines.push(`实验标题：${baseline.title} → ${profile.title}`);
  }
  if (profile.agent_a.display_name !== baseline.agent_a.display_name) {
    lines.push(`Agent A 名称：${baseline.agent_a.display_name} → ${profile.agent_a.display_name}`);
  }
  if (profile.agent_b.display_name !== baseline.agent_b.display_name) {
    lines.push(`Agent B 名称：${baseline.agent_b.display_name} → ${profile.agent_b.display_name}`);
  }
  if (profile.agent_a.model.provider !== baseline.agent_a.model.provider || profile.agent_a.model.model !== baseline.agent_a.model.model) {
    lines.push(`Agent A 模型：${baseline.agent_a.model.provider}/${baseline.agent_a.model.model} → ${profile.agent_a.model.provider}/${profile.agent_a.model.model}`);
  }
  if (profile.agent_b.model.provider !== baseline.agent_b.model.provider || profile.agent_b.model.model !== baseline.agent_b.model.model) {
    lines.push(`Agent B 模型：${baseline.agent_b.model.provider}/${baseline.agent_b.model.model} → ${profile.agent_b.model.provider}/${profile.agent_b.model.model}`);
  }
  if (profile.prompt_version !== baseline.prompt_version) {
    lines.push(`Prompt 版本：${baseline.prompt_version} → ${profile.prompt_version}`);
  }
  const projectionModule = profile.modules.projection?.module_id;
  const baseProjectionModule = baseline.modules.projection?.module_id;
  if (projectionModule !== baseProjectionModule) {
    lines.push(`Projection 模块：${baseProjectionModule ?? "-"} → ${projectionModule ?? "-"}`);
  }
  return lines.length > 0 ? lines : ["当前配置与模板基线一致。"];
}

function preflightWizardValidation(
  profile: ExperimentProfile | null,
  credentials: CredentialRead[],
): string[] {
  if (!profile) {
    return ["尚未选择模板。"];
  }
  const issues: string[] = [];
  for (const [label, agent] of [
    ["Agent A", profile.agent_a],
    ["Agent B", profile.agent_b],
  ] as const) {
    if (!agent.display_name.trim()) {
      issues.push(`${label} 需要显示名。`);
    }
    if (!agent.persona_prompt.trim()) {
      issues.push(`${label} 需要 persona。`);
    }
    if (agent.model.provider === "openai-compatible" && !agent.model.credential_ref) {
      issues.push(`${label} 使用 openai-compatible 时必须选择 credential_ref。`);
    }
    if (agent.model.credential_ref && !credentials.some((item) => item.credential_ref === agent.model.credential_ref)) {
      issues.push(`${label} 指向了不存在的 credential_ref。`);
    }
  }
  if (!profile.modules.message_sequence?.module_id) {
    issues.push("缺少消息序列模块。");
  }
  if (!profile.modules.projection?.module_id) {
    issues.push("缺少 projection 模块。");
  }
  return issues;
}

function RawJsonCard({ title, value }: { title: string; value: unknown }): JSX.Element {
  return (
    <details className="panel-card secondary-card">
      <summary>{title}</summary>
      <pre className="json-fallback">{asJson(value)}</pre>
    </details>
  );
}

export function App(): JSX.Element {
  const serverInspectorCacheRef = useRef<Record<string, Awaited<ReturnType<typeof loadServerInspector>>>>({});
  const runtimeStateRef = useRef(emptyRuntimeViewState());
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const pendingSidebarInteractionRef = useRef<{ interactionId: string; conversationId: string } | null>(
    null,
  );
  const composerRef = useRef<HTMLTextAreaElement | null>(null);

  const [apiBase, setApiBase] = useState(fallbackUrl());
  const [bootstrap, setBootstrap] = useState<BootstrapResponse | null>(null);
  const [templates, setTemplates] = useState<BootstrapResponse["templates"]>([]);
  const [credentials, setCredentials] = useState<CredentialRead[]>([]);
  const [conversations, setConversations] = useState<ConversationRead[]>([]);
  const [historyEntries, setHistoryEntries] = useState<HistoryEntry[]>([]);
  const [selectedConversationId, setSelectedConversationId] = useState("");
  const [conversation, setConversation] = useState<ConversationRead | null>(null);
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [cpRevisions, setCpRevisions] = useState<CpRevision[]>([]);
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [costs, setCosts] = useState<CostBreakdown | null>(null);
  const [automaticMetrics, setAutomaticMetrics] = useState<AutomaticMetrics | null>(null);
  const [manualScore, setManualScore] = useState<ManualScore | null>(null);
  const [historyDetail, setHistoryDetail] = useState<HistoryDetail | null>(null);
  const [selectedHistoryId, setSelectedHistoryId] = useState("");
  const [draftProfile, setDraftProfile] = useState<ExperimentProfile | null>(null);
  const [guardrailsDraft, setGuardrailsDraft] = useState<RuntimeGuardrails | null>(null);
  const [selectedInspector, setSelectedInspector] = useState<InspectorView>("server");
  const [selectedRunId, setSelectedRunId] = useState("");
  const [selectedMessageId, setSelectedMessageId] = useState("");
  const [reviewState, setReviewState] = useState<ReviewState | null>(null);
  const [runtimeState, setRuntimeState] = useState(emptyRuntimeViewState());
  const [needsReviewResync, setNeedsReviewResync] = useState(false);
  const [socketNonce, setSocketNonce] = useState(0);
  const [wsBanner, setWsBanner] = useState<string | null>(null);
  const [errorText, setErrorText] = useState<string | null>(null);
  const [statusText, setStatusText] = useState("初始化中");
  const [visibleMessageLimit, setVisibleMessageLimit] = useState(200);
  const [memoryByAgent, setMemoryByAgent] = useState<Record<string, MemoryRevision[]>>({});
  const [contextByAgent, setContextByAgent] = useState<Record<string, ContextBundle[]>>({});
  const [attemptsByAgent, setAttemptsByAgent] = useState<Record<string, AgentRun["attempts"]>>({});
  const [exportJob, setExportJob] = useState<AnalysisExportJob | null>(null);
  const [composer, setComposer] = useState("");
  const [mentionState, setMentionState] = useState<MentionState | null>(null);
  const [mentionIndex, setMentionIndex] = useState(0);
  const [replyToId, setReplyToId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("新的实验");
  const [draftTemplateId, setDraftTemplateId] = useState("");
  const [wizardOpen, setWizardOpen] = useState(false);
  const [wizardStepIndex, setWizardStepIndex] = useState(0);
  const [wizardProfile, setWizardProfile] = useState<ExperimentProfile | null>(null);
  const [wizardGuardrails, setWizardGuardrails] = useState<RuntimeGuardrails>({
    max_llm_calls: 32,
    max_total_tokens: 64_000,
    max_auto_retries: 2,
    worker_restart_limit: 1,
    pause: false,
    stop: false,
    log_level: "info",
  });
  const [wizardValidation, setWizardValidation] = useState<ValidationResult | null>(null);
  const [wizardSubmitting, setWizardSubmitting] = useState(false);
  const [dragState, setDragState] = useState<DragState>(null);
  const [leftPaneRatio, setLeftPaneRatio] = useState(0.33);
  const [upperPaneRatio, setUpperPaneRatio] = useState(0.44);
  const [credentialForm, setCredentialForm] = useState({
    provider: "openai-compatible",
    label: "",
    secret: "",
  });
  const [metadataTitle, setMetadataTitle] = useState("");
  const [metadataTags, setMetadataTags] = useState("");

  const profiles = agentProfiles(conversation);
  const currentProfile = activeProfile(conversation);
  const liveReadOnly = reviewState !== null || conversation?.status !== "running";
  const configLocked = conversation?.status !== "draft";
  const mentionOptions = mentionCandidates(profiles);
  const filteredMentionOptions = mentionState
    ? mentionOptions.filter((item) => item.token.toLowerCase().includes(`@${mentionState.query}`))
    : [];
  const replyTarget = messages.find((message) => message.message_id === replyToId) ?? null;
  const cpReviewRevision =
    reviewState?.kind === "cp" && reviewState.cpRevision !== undefined
      ? reviewState.cpRevision
      : null;
  const visibleMessages =
    cpReviewRevision !== null
      ? messages.filter((item) => item.cp_revision <= cpReviewRevision)
      : messages;
  const renderedMessages = visibleMessages.slice(-visibleMessageLimit);
  const latestCp = cpRevisions.at(-1) ?? null;
  const wizardStep = WIZARD_STEPS[wizardStepIndex];
  const wizardTemplateProfile = templateProfile(templates, draftTemplateId);
  const wizardPreflightIssues = preflightWizardValidation(wizardProfile, credentials);
  const wizardDiffLines =
    wizardProfile || wizardTemplateProfile
      ? summarizeProfileDiff(wizardProfile ?? wizardTemplateProfile!, wizardTemplateProfile)
      : ["尚未选择模板。"];

  function syncRuntimeConversation(
    detail: ConversationRead,
    authority: RuntimeAuthority | null,
    messageItems: Message[],
  ): void {
    setConversation(detail);
    setDraftProfile(enableUiRuntime(cloneProfile(detail.draft_profile)));
    setGuardrailsDraft(detail.guardrails);
    setMetadataTitle(String(detail.catalog_metadata.title_override ?? detail.title));
    setMetadataTags(tagsText(detail.catalog_metadata.tags));
    setRuntimeState({
      ...emptyRuntimeViewState(),
      messages: messageItems,
      cpRevisions: [],
      runs: [],
      latestSeq: messageItems.at(-1)?.conversation_seq ?? 0,
      latestCpRevision: messageItems.at(-1)?.cp_revision ?? 0,
      guardrails: detail.guardrails,
      typing: typingFromAuthority(authority),
    });
  }

  async function refreshSidebarLists(): Promise<void> {
    const [conversationItems, historyItems] = await Promise.all([
      listConversations(apiBase),
      listHistory(apiBase),
    ]);
    setConversations(conversationItems);
    setHistoryEntries(historyItems);
    if (!selectedConversationId) {
      const target =
        conversationItems.find((item) => item.status === "running") ??
        conversationItems.find((item) => item.status === "draft") ??
        conversationItems[0] ??
        null;
      if (target) {
        setSelectedConversationId(target.id);
      }
    }
  }

  async function refreshBootstrapData(): Promise<void> {
    const payload = await loadBootstrap(apiBase);
    setBootstrap(payload);
    setTemplates(payload.templates);
    setCredentials(payload.credentials);
    setDraftTemplateId((current) => current || payload.templates[0]?.id || "");
  }

  async function refreshCredentials(): Promise<void> {
    setCredentials(await listCredentials(apiBase));
  }

  async function refreshConversation(conversationId: string): Promise<void> {
    const [workbench, detail] = await Promise.all([
      loadWorkbench(apiBase, conversationId),
      loadConversationDetail(apiBase, conversationId),
    ]);
    syncRuntimeConversation(
      workbench.detail,
      workbench.runtimeAuthority ?? detail.runtime_authority,
      workbench.messages,
    );
    setValidation(detail.validation);
    setStatusText("已同步");
    setVisibleMessageLimit(200);
  }

  async function refreshCurrentConversation(): Promise<void> {
    if (!selectedConversationId) {
      return;
    }
    await refreshConversation(selectedConversationId);
    await refreshSidebarLists();
  }

  useEffect(() => {
    runtimeStateRef.current = runtimeState;
  }, [runtimeState]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        setErrorText(null);
        setStatusText("加载实验台");
        await Promise.all([refreshBootstrapData(), refreshSidebarLists()]);
        if (!cancelled) {
          setStatusText("已连接");
        }
      } catch (error) {
        if (!cancelled) {
          setErrorText(error instanceof Error ? error.message : "初始化失败");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [apiBase]);

  useEffect(() => {
    if (!wizardOpen) {
      return;
    }
    const nextProfile = templateProfile(templates, draftTemplateId);
    if (!nextProfile) {
      return;
    }
    setWizardProfile(enableUiRuntime(nextProfile));
    setWizardValidation(null);
  }, [draftTemplateId, templates, wizardOpen]);

  useEffect(() => {
    if (!selectedConversationId) {
      return;
    }
    void refreshConversation(selectedConversationId).catch((error: unknown) => {
      setErrorText(error instanceof Error ? error.message : "同步失败");
    });
  }, [apiBase, selectedConversationId]);

  useEffect(() => {
    if (!selectedConversationId) {
      return;
    }
    if (conversation?.archive_dir === null && conversation?.status === "draft") {
      return;
    }
    if (socketRef.current) {
      socketRef.current.close();
    }
    const url = new URL(`/ws/v1/conversations/${selectedConversationId}`, apiBase);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("after_seq", String(runtimeStateRef.current.latestSeq));
    const socket = new WebSocket(url);
    socketRef.current = socket;
    socket.addEventListener("open", () => setWsBanner(null));
    socket.addEventListener("message", (incoming) => {
      const payload = JSON.parse(incoming.data) as OutboxEvent | SessionSnapshot;
      if (reviewState !== null) {
        setNeedsReviewResync(true);
      }
      setRuntimeState((current) => applySocketPayload(current, payload, reviewState));
    });
    socket.addEventListener("close", () => {
      setWsBanner("连接已断开，正在补取缺口并重连。");
      reconnectTimerRef.current = window.setTimeout(() => {
        void refreshConversation(selectedConversationId).catch(() => undefined);
        setSocketNonce((current) => current + 1);
      }, 5000);
    });
    return () => {
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
      }
      socket.close();
    };
  }, [apiBase, conversation?.archive_dir, conversation?.status, reviewState, selectedConversationId, socketNonce]);

  useEffect(() => {
    setMessages(runtimeState.messages);
  }, [runtimeState.messages]);

  useEffect(() => {
    if (runtimeState.guardrails) {
      setGuardrailsDraft(runtimeState.guardrails);
    }
  }, [runtimeState.guardrails]);

  useEffect(() => {
    if (!conversation?.id) {
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void loadServerInspector(apiBase, conversation.id)
        .then((payload) => {
          if (cancelled) {
            return;
          }
          serverInspectorCacheRef.current[conversation.id] = payload;
          setCpRevisions(payload.cpRevisions);
          setRuns(payload.runs);
          setLogs(payload.logs);
          setCosts(payload.costs);
          setAutomaticMetrics(payload.automaticMetrics);
          setManualScore(normalizeManualScore(payload.manualScore));
          setRuntimeState((current) => ({
            ...current,
            cpRevisions: payload.cpRevisions,
            runs: payload.runs,
            latestCpRevision: payload.cpRevisions.at(-1)?.projection_revision ?? current.latestCpRevision,
          }));
        })
        .catch((error: unknown) => {
          if (!cancelled) {
            setErrorText(error instanceof Error ? error.message : "公共视图读取失败");
          }
        });
    }, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [apiBase, conversation?.archive_dir, conversation?.id, conversation?.status]);

  useEffect(() => {
    if (dragState === null) {
      return;
    }
    const activeDrag = dragState;
    function handlePointerMove(event: PointerEvent): void {
      if (activeDrag.type === "main") {
        const nextRatio = Math.min(
          0.48,
          Math.max(0.24, activeDrag.initialRatio + (event.clientX - activeDrag.startX) / window.innerWidth),
        );
        setLeftPaneRatio(nextRatio);
      } else {
        const nextRatio = Math.min(
          0.68,
          Math.max(0.28, activeDrag.initialRatio + (event.clientY - activeDrag.startY) / window.innerHeight),
        );
        setUpperPaneRatio(nextRatio);
      }
    }
    function handlePointerUp(): void {
      setDragState(null);
    }
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp);
    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
    };
  }, [dragState]);

  useEffect(() => {
    if (!conversation || !profiles || selectedInspector === "server") {
      return;
    }
    const agent = selectedInspector === "agent-a" ? profiles[0] : profiles[1];
    if (
      memoryByAgent[agent.agent_id] !== undefined &&
      contextByAgent[agent.agent_id] !== undefined &&
      attemptsByAgent[agent.agent_id] !== undefined
    ) {
      return;
    }
    void loadAgentInspector(apiBase, conversation.id, agent.agent_id)
      .then((payload) => {
        setMemoryByAgent((current) => ({ ...current, [agent.agent_id]: payload.memory }));
        setContextByAgent((current) => ({ ...current, [agent.agent_id]: payload.contextBundles }));
        setAttemptsByAgent((current) => ({ ...current, [agent.agent_id]: payload.attempts }));
      })
      .catch((error: unknown) => {
        setErrorText(error instanceof Error ? error.message : "私有视图读取失败");
      });
  }, [apiBase, attemptsByAgent, contextByAgent, conversation, memoryByAgent, profiles, selectedInspector]);

  useEffect(() => {
    const pendingInteraction = pendingSidebarInteractionRef.current;
    if (!pendingInteraction || !conversation || conversation.id !== pendingInteraction.conversationId) {
      return;
    }
    let cancelled = false;
    void markInteractionPainted(pendingInteraction.interactionId, "sidebar_conversation_painted").then(() => {
      if (!cancelled && pendingSidebarInteractionRef.current?.interactionId === pendingInteraction.interactionId) {
        pendingSidebarInteractionRef.current = null;
      }
    });
    return () => {
      cancelled = true;
    };
  }, [conversation, messages]);

  useEffect(() => {
    if (!selectedHistoryId) {
      return;
    }
    void loadHistoryDetail(apiBase, selectedHistoryId)
      .then((payload) => setHistoryDetail(payload))
      .catch((error: unknown) => {
        setErrorText(error instanceof Error ? error.message : "历史档案读取失败");
      });
  }, [apiBase, selectedHistoryId]);

  useEffect(() => {
    if (!conversation || !exportJob || exportJob.status === "ready" || exportJob.status === "failed") {
      return;
    }
    const timer = window.setTimeout(() => {
      void getAnalysisExport(apiBase, conversation.id, exportJob.job_id)
        .then((payload) => setExportJob(payload))
        .catch(() => undefined);
    }, 800);
    return () => window.clearTimeout(timer);
  }, [apiBase, conversation, exportJob]);

  function handleConversationSelect(conversationId: string, title: string): void {
    if (conversationId === selectedConversationId) {
      return;
    }
    const interactionId = beginUiInteraction("sidebar_select", {
      conversation_id: conversationId,
      title,
    });
    pendingSidebarInteractionRef.current =
      interactionId === null ? null : { interactionId, conversationId };
    setSelectedConversationId(conversationId);
    setSelectedHistoryId("");
    setHistoryDetail(null);
  }

  function updateAgentDraft(which: "agent_a" | "agent_b", patch: Partial<AgentProfile>): void {
    if (!draftProfile) {
      return;
    }
    setDraftProfile({
      ...draftProfile,
      [which]: {
        ...draftProfile[which],
        ...patch,
      },
    });
  }

  function applyMention(option: MentionOption): void {
    if (!mentionState) {
      return;
    }
    const next = `${composer.slice(0, mentionState.start)}${option.token} ${composer.slice(mentionState.end)}`;
    setComposer(next);
    setMentionState(null);
    queueMicrotask(() => {
      composerRef.current?.focus();
      const cursor = mentionState.start + option.token.length + 1;
      composerRef.current?.setSelectionRange(cursor, cursor);
    });
  }

  function openWizard(): void {
    if (!draftTemplateId && templates.length === 0) {
      setErrorText("当前没有可用模板。");
      return;
    }
    const nextTemplateId = draftTemplateId || templates[0]?.id || "";
    const nextProfile = templateProfile(templates, nextTemplateId);
    if (!nextProfile) {
      setErrorText("无法读取模板配置。");
      return;
    }
    setDraftTemplateId(nextTemplateId);
    setDraftTitle("新的实验");
    setWizardStepIndex(0);
    setWizardProfile(enableUiRuntime(nextProfile));
    setWizardGuardrails({
      max_llm_calls: 32,
      max_total_tokens: 64_000,
      max_auto_retries: 2,
      worker_restart_limit: 1,
      pause: false,
      stop: false,
      log_level: "info",
    });
    setWizardValidation(null);
    setWizardOpen(true);
  }

  function updateWizardProfile(recipe: (current: ExperimentProfile) => ExperimentProfile): void {
    setWizardProfile((current) => (current ? recipe(current) : current));
  }

  function goWizardStep(direction: 1 | -1): void {
    setWizardStepIndex((current) => Math.min(WIZARD_STEPS.length - 1, Math.max(0, current + direction)));
  }

  async function handleWizardCreateDraft(): Promise<void> {
    if (!wizardProfile) {
      setErrorText("向导配置未就绪。");
      return;
    }
    try {
      setWizardSubmitting(true);
      setStatusText("创建草稿");
      const profile = enableUiRuntime({
        ...wizardProfile,
        title: draftTitle.trim() || wizardProfile.title,
      });
      const created = await createConversation(apiBase, {
        title: draftTitle.trim() || "新的实验",
        draft_profile: profile,
      });
      await updateGuardrails(apiBase, created.id, wizardGuardrails);
      const validationResult = await validateConversation(apiBase, created.id);
      setWizardValidation(validationResult);
      await refreshBootstrapData();
      await refreshSidebarLists();
      setSelectedConversationId(created.id);
      setWizardOpen(false);
      setStatusText(validationResult.ok ? "草稿已创建并完成校验" : "草稿已创建，但校验有问题");
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "创建草稿失败");
    } finally {
      setWizardSubmitting(false);
    }
  }

  async function handleSaveDraft(): Promise<void> {
    if (!conversation || !draftProfile) {
      return;
    }
    try {
      const saved = await updateDraftProfile(
        apiBase,
        conversation.id,
        enableUiRuntime(draftProfile),
      );
      setConversation(saved);
      setDraftProfile(cloneProfile(saved.draft_profile));
      setStatusText("草稿已保存");
      await refreshSidebarLists();
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "保存草稿失败");
    }
  }

  async function handleValidateConversation(): Promise<void> {
    if (!conversation) {
      return;
    }
    try {
      const payload = await validateConversation(apiBase, conversation.id);
      setValidation(payload);
      setStatusText(payload.ok ? "兼容校验通过" : "兼容校验有问题");
      await refreshSidebarLists();
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "兼容校验失败");
    }
  }

  async function handleConversationAction(action: "start" | "pause" | "resume" | "end"): Promise<void> {
    if (!conversation) {
      return;
    }
    try {
      const updated = await conversationAction(apiBase, conversation.id, action);
      setConversation(updated);
      setStatusText(`实验已${action === "start" ? "开始" : action === "pause" ? "暂停" : action === "resume" ? "恢复" : "结束"}`);
      await refreshCurrentConversation();
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "状态切换失败");
    }
  }

  async function handleCloneConversation(): Promise<void> {
    if (!conversation) {
      return;
    }
    try {
      const profile = enableUiRuntime(await cloneConversationProfile(apiBase, conversation.id));
      profile.title = `${conversation.title} 复制`;
      const created = await createConversation(apiBase, {
        title: `${conversation.title} 复制`,
        draft_profile: profile,
      });
      await refreshSidebarLists();
      setSelectedConversationId(created.id);
      setStatusText("已复制配置");
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "复制配置失败");
    }
  }

  async function handleSaveMetadata(): Promise<void> {
    if (!conversation) {
      return;
    }
    try {
      const updated = await updateConversationMetadata(apiBase, conversation.id, {
        title_override: metadataTitle.trim(),
        tags: parseTags(metadataTags),
      });
      setConversation(updated);
      setStatusText("目录标题与标签已保存");
      await refreshSidebarLists();
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "目录信息保存失败");
    }
  }

  async function handleCreateCredential(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    try {
      await createCredential(apiBase, credentialForm);
      setCredentialForm({ provider: credentialForm.provider, label: "", secret: "" });
      await refreshCredentials();
      setStatusText("凭据已创建");
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "凭据创建失败");
    }
  }

  async function handleCredentialValidate(credentialRef: string): Promise<void> {
    try {
      await validateCredential(apiBase, credentialRef);
      await refreshCredentials();
      setStatusText("凭据已验证");
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "凭据验证失败");
    }
  }

  async function handleCredentialDelete(credentialRef: string): Promise<void> {
    try {
      await deleteCredential(apiBase, credentialRef);
      await refreshCredentials();
      setStatusText("凭据已删除");
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "凭据删除失败");
    }
  }

  async function handleGuardrailSave(): Promise<void> {
    if (!conversation || !guardrailsDraft) {
      return;
    }
    try {
      const updated = await updateGuardrails(apiBase, conversation.id, guardrailsDraft);
      setConversation(updated);
      setGuardrailsDraft(updated.guardrails);
      setStatusText("运行护栏已更新");
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "运行护栏更新失败");
    }
  }

  async function handleManualScoreSave(): Promise<void> {
    if (!conversation || !manualScore) {
      return;
    }
    try {
      const payload = await saveManualScore(apiBase, conversation.id, {
        rubric_version: manualScore.rubric_version,
        scores: manualScore.scores,
        overall_note: manualScore.overall_note,
      });
      setManualScore(normalizeManualScore(payload));
      setStatusText("人工评分已保存");
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "人工评分保存失败");
    }
  }

  async function handleCreateExport(): Promise<void> {
    if (!conversation) {
      return;
    }
    try {
      const payload = await createAnalysisExport(apiBase, conversation.id);
      setExportJob(payload);
      setStatusText("分析导出已排队");
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "分析导出创建失败");
    }
  }

  async function handleRetryMessage(message: Message): Promise<void> {
    if (!conversation || !message.client_message_id) {
      return;
    }
    try {
      const payload = await retryMessage(apiBase, conversation.id, message.client_message_id);
      setMessages((current) => upsertMessage(current, { ...payload, ui_status: "sent" }));
      setStatusText("消息已重试");
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "消息重试失败");
    }
  }

  async function handleSubmitMessage(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!conversation || !composer.trim()) {
      return;
    }
    if (reviewState !== null || conversation.status !== "running") {
      setErrorText("回看或只读模式下禁止写请求。");
      return;
    }
    const clientMessageId = `web-${Date.now()}`;
    const optimistic: Message = {
      message_id: clientMessageId,
      conversation_seq: runtimeState.latestSeq + 1,
      sender_kind: "user",
      sender_id: "user",
      content_markdown: composer,
      mentions: collectMentions(composer, mentionOptions),
      primary_reply_to: replyToId,
      responds_to: replyToId ? [replyToId] : [],
      client_message_id: clientMessageId,
      causal_episode_id: null,
      caused_by_message_id: null,
      agent_hop: 0,
      committed_at: new Date().toISOString(),
      cp_revision: runtimeState.latestCpRevision,
      ui_status: "pending",
    };
    setMessages((current) => upsertMessage(current, optimistic));
    setComposer("");
    setMentionState(null);
    setReplyToId(null);
    try {
      const payload = await submitMessage(apiBase, conversation.id, {
        client_message_id: clientMessageId,
        content_markdown: optimistic.content_markdown,
        mentions: optimistic.mentions,
        responds_to: optimistic.responds_to,
        primary_reply_to: optimistic.primary_reply_to,
      });
      setMessages((current) => upsertMessage(current, { ...payload, ui_status: "sent" }));
      await refreshConversation(conversation.id);
    } catch (error) {
      setMessages((current) =>
        current.map((item) =>
          item.client_message_id === clientMessageId ? { ...item, ui_status: "failed" } : item,
        ),
      );
      setErrorText(error instanceof Error ? error.message : "发送失败");
    }
  }

  function exitReview(): void {
    setReviewState(null);
    if (selectedConversationId && needsReviewResync) {
      setNeedsReviewResync(false);
      void refreshConversation(selectedConversationId).catch(() => undefined);
    }
  }

  function handleMessageSelect(message: Message): void {
    setSelectedMessageId(message.message_id);
    const run = inferRunForMessage(runs, message);
    if (run) {
      setSelectedRunId(run.run_id);
      if (profiles?.[0].agent_id === run.agent_id) {
        setSelectedInspector("agent-a");
      }
      if (profiles?.[1].agent_id === run.agent_id) {
        setSelectedInspector("agent-b");
      }
    }
  }

  const selectedServerLogs = logs.slice(-12);

  return (
    <div className="shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">pal-chat / August 2, 2026</p>
          <h1>群聊实验台</h1>
          <p className="subtle">
            从前端完成凭据配置、实验创建、运行观察、结束评分和历史回看。
          </p>
        </div>
        <label className="endpoint">
          <span>API Base</span>
          <input aria-label="API Base" value={apiBase} onChange={(event) => setApiBase(event.target.value)} />
        </label>
        <div className="status-strip" aria-live="polite">
          <span>{statusText}</span>
          <span>{conversation ? `${conversation.status} · seq ${runtimeState.latestSeq}` : "未选择实验"}</span>
          <span>{bootstrap ? `模板 ${templates.length} · 凭据 ${credentials.length}` : "加载中"}</span>
        </div>
      </header>

      {bootstrap?.note ? <div className="banner">{bootstrap.note}</div> : null}
      {reviewState ? (
        <div className="banner warning" role="alert">
          <strong>回看模式</strong>
          <span>{reviewState.label}</span>
          <button type="button" onClick={exitReview}>
            退出并补齐事件
          </button>
        </div>
      ) : null}
      {wsBanner ? <div className="banner">{wsBanner}</div> : null}
      {errorText ? <div className="banner error" role="alert">{errorText}</div> : null}

      <main
        className="workbench"
        style={{
          gridTemplateColumns: `minmax(24rem, ${Math.round(leftPaneRatio * 100)}%) 12px minmax(0, 1fr)`,
        }}
      >
        <section className="chat-pane">
          <div className="pane-head">
            <div>
              <p className="eyebrow">公共聊天</p>
              <h2>{conversation?.title ?? "先创建一个实验草稿"}</h2>
              <p className="subtle">
                支持引用、@A/@B/@all、乐观发送、失败重试和断线补取。
              </p>
            </div>
            <span className={liveReadOnly ? "mode-pill readonly" : "mode-pill"}>
              {liveReadOnly ? "只读" : "运行中"}
            </span>
          </div>

          {replyTarget ? (
            <div className="reply-banner">
              <span>引用：{messageSummary(replyTarget)}</span>
              <button type="button" onClick={() => setReplyToId(null)}>
                取消
              </button>
            </div>
          ) : null}

          {visibleMessages.length > renderedMessages.length ? (
            <div className="message-window-banner">
              <span>显示最近 {renderedMessages.length} / {visibleMessages.length} 条消息</span>
              <button type="button" onClick={() => setVisibleMessageLimit((current) => current + 200)}>
                加载更早消息
              </button>
            </div>
          ) : null}

          <div className="message-log" role="log" aria-live="polite">
            {renderedMessages.length === 0 ? (
              <div className="empty-state">
                <strong>还没有公共消息</strong>
                <span>先完成右侧配置并开始实验，然后在这里发出第一条消息。</span>
              </div>
            ) : null}
            {renderedMessages.map((message) => (
              <article
                key={message.message_id}
                className={message.message_id === selectedMessageId ? "message-card active" : "message-card"}
              >
                <button type="button" className="message-select" onClick={() => handleMessageSelect(message)}>
                  <div className="message-head">
                    <span>#{message.conversation_seq}</span>
                    <span>{messageDisplayName(message, profiles)}</span>
                    <span>{message.ui_status ?? "sent"}</span>
                  </div>
                  {message.primary_reply_to ? (
                    <div className="message-quote">回复消息：{message.primary_reply_to}</div>
                  ) : null}
                  <div className="message-body">{message.content_markdown}</div>
                  <div className="message-meta">
                    <span>CP {message.cp_revision}</span>
                    <span>{formatTime(message.committed_at)}</span>
                    {message.mentions.length > 0 ? <span>{message.mentions.join(", ")}</span> : null}
                  </div>
                </button>
                <div className="message-actions">
                  <button type="button" onClick={() => setReplyToId(message.message_id)}>
                    引用
                  </button>
                  {message.ui_status === "failed" && message.client_message_id ? (
                    <button type="button" onClick={() => void handleRetryMessage(message)}>
                      重试
                    </button>
                  ) : null}
                </div>
              </article>
            ))}
            {profiles
              ?.map((agent) => ({ agent, typing: runtimeState.typing[agent.agent_id] }))
              .filter((item) => item.typing?.active)
              .map((item) => (
                <div key={item.agent.agent_id} className="typing-card">
                  <strong>{item.agent.display_name}</strong>
                  <span>正在输入…</span>
                </div>
              ))}
          </div>

          <form className="composer" onSubmit={(event) => void handleSubmitMessage(event)}>
            <textarea
              ref={composerRef}
              aria-label="Public Message"
              value={composer}
              onChange={(event) => {
                setComposer(event.target.value);
                setMentionState(detectMention(event.target.value, event.target.selectionStart ?? event.target.value.length));
                setMentionIndex(0);
              }}
              onClick={(event) =>
                setMentionState(detectMention(composer, (event.target as HTMLTextAreaElement).selectionStart ?? composer.length))
              }
              onKeyDown={(event) => {
                if (mentionState && filteredMentionOptions.length > 0) {
                  if (event.key === "ArrowDown") {
                    event.preventDefault();
                    setMentionIndex((current) => (current + 1) % filteredMentionOptions.length);
                    return;
                  }
                  if (event.key === "ArrowUp") {
                    event.preventDefault();
                    setMentionIndex((current) => (current - 1 + filteredMentionOptions.length) % filteredMentionOptions.length);
                    return;
                  }
                  if (event.key === "Tab" || event.key === "Enter") {
                    event.preventDefault();
                    applyMention(filteredMentionOptions[mentionIndex] ?? filteredMentionOptions[0]);
                    return;
                  }
                }
                if (event.key === "Escape") {
                  setMentionState(null);
                  if (!mentionState) {
                    setComposer("");
                  }
                }
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void handleSubmitMessage(event as unknown as FormEvent<HTMLFormElement>);
                }
              }}
              disabled={liveReadOnly}
              placeholder={liveReadOnly ? "当前状态只读" : "输入消息，Enter 发送，Shift+Enter 换行"}
            />
            {mentionState && filteredMentionOptions.length > 0 ? (
              <div className="mention-menu" role="listbox" aria-label="Mention Suggestions">
                {filteredMentionOptions.map((option, index) => (
                  <button
                    key={option.token}
                    type="button"
                    className={index === mentionIndex ? "mention-option active" : "mention-option"}
                    onClick={() => applyMention(option)}
                  >
                    <strong>{option.token}</strong>
                    <span>{option.description}</span>
                  </button>
                ))}
              </div>
            ) : null}
            <button type="submit" disabled={liveReadOnly || !composer.trim()}>
              发送
            </button>
          </form>
        </section>

        <div
          className="drag-divider vertical"
          role="separator"
          aria-label="主分隔线"
          aria-orientation="vertical"
          aria-valuemin={24}
          aria-valuemax={48}
          aria-valuenow={Math.round(leftPaneRatio * 100)}
          onPointerDown={(event) => setDragState({ type: "main", startX: event.clientX, initialRatio: leftPaneRatio })}
        />

        <section className="right-pane">
          <section className="hero-card">
            <div>
              <p className="eyebrow">全局控制</p>
              <h2>{metadataTitle || conversation?.title || "未命名实验"}</h2>
              <p className="subtle">
                新建草稿、校验兼容、开始实验、暂停/恢复、结束、复制配置、导出分析都集中在这里。
              </p>
            </div>
            <div className="toolbar-actions">
              <button type="button" onClick={() => void handleValidateConversation()} disabled={!conversation}>
                校验配置
              </button>
              <button type="button" onClick={() => void handleConversationAction("start")} disabled={!conversation || conversation.status === "running"}>
                开始实验
              </button>
              <button type="button" onClick={() => void handleConversationAction("pause")} disabled={!conversation || conversation.status !== "running"}>
                暂停
              </button>
              <button type="button" onClick={() => void handleConversationAction("resume")} disabled={!conversation || conversation.status !== "paused"}>
                恢复
              </button>
              <button type="button" onClick={() => void handleConversationAction("end")} disabled={!conversation || !["running", "paused"].includes(conversation.status)}>
                结束
              </button>
              <button type="button" onClick={() => void handleCloneConversation()} disabled={!conversation}>
                复制配置
              </button>
              <button type="button" onClick={() => void handleCreateExport()} disabled={!conversation}>
                导出分析
              </button>
            </div>
          </section>

          <section className="setup-grid">
            <section className="panel-card">
              <header className="panel-card-head">
                <div>
                  <h3>首次配置与凭据</h3>
                  <p className="subtle">真实模型只保存 `credential_ref`；界面只显示掩码和验证时间。</p>
                </div>
              </header>
              <form className="stack-form" onSubmit={(event) => void handleCreateCredential(event)}>
                <label>
                  <span>Provider</span>
                  <input
                    aria-label="Credential Provider"
                    value={credentialForm.provider}
                    onChange={(event) =>
                      setCredentialForm((current) => ({ ...current, provider: event.target.value }))
                    }
                  />
                </label>
                <label>
                  <span>标签</span>
                  <input
                    aria-label="Credential Label"
                    value={credentialForm.label}
                    onChange={(event) =>
                      setCredentialForm((current) => ({ ...current, label: event.target.value }))
                    }
                  />
                </label>
                <label>
                  <span>密钥</span>
                  <input
                    aria-label="Credential Secret"
                    type="password"
                    value={credentialForm.secret}
                    onChange={(event) =>
                      setCredentialForm((current) => ({ ...current, secret: event.target.value }))
                    }
                  />
                </label>
                <button type="submit">创建凭据</button>
              </form>
              <div className="conversation-list">
                {credentials.length === 0 ? (
                  <div className="empty-inline">还没有凭据。若仅做验收，可继续使用 Scripted Adapter。</div>
                ) : null}
                {credentials.map((credential) => (
                  <div key={credential.credential_ref} className="credential-row">
                    <div>
                      <strong>{credential.label}</strong>
                      <span>{credential.provider}</span>
                      <span>{credential.masked_value}</span>
                      <span>最近验证：{formatTime(credential.validated_at)}</span>
                    </div>
                    <div className="row-actions">
                      <button type="button" onClick={() => void handleCredentialValidate(credential.credential_ref)}>
                        验证
                      </button>
                      <button type="button" onClick={() => void handleCredentialDelete(credential.credential_ref)}>
                        删除
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </section>

            <section className="panel-card">
              <header className="panel-card-head">
                <div>
                  <h3>新建实验草稿</h3>
                  <p className="subtle">使用 9 步向导完成模板、角色、模型、Attention、Projection、护栏、校验和启动确认。</p>
                </div>
              </header>
              <div className="stack-form">
                <label>
                  <span>模板</span>
                  <select
                    aria-label="Template Select"
                    value={draftTemplateId}
                    onChange={(event) => setDraftTemplateId(event.target.value)}
                  >
                    {templates.map((template) => (
                      <option key={template.id} value={template.id}>
                        {template.title}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>实验标题</span>
                  <input aria-label="Draft Title" value={draftTitle} onChange={(event) => setDraftTitle(event.target.value)} />
                </label>
                <button type="button" onClick={openWizard}>打开 9 步向导</button>
              </div>
              <div className="conversation-list" role="list">
                {conversations.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className={item.id === selectedConversationId ? "conversation-item active" : "conversation-item"}
                    onClick={() => handleConversationSelect(item.id, item.title)}
                  >
                    <strong>{item.title}</strong>
                    <span>{item.status}</span>
                    <span>{formatTime(item.updated_at)}</span>
                  </button>
                ))}
              </div>
            </section>
          </section>

          <div
            className="observation-shell"
            style={{ gridTemplateRows: `minmax(20rem, ${Math.round(upperPaneRatio * 100)}%) 12px minmax(22rem, 1fr)` }}
          >
            <section className="agent-panels">
              {profiles ? (
                <>
                  <AgentConfigCard
                    title={profiles[0].display_name}
                    agent={draftProfile?.agent_a ?? profiles[0]}
                    credentials={credentials}
                    locked={configLocked}
                    typingState={runtimeState.typing[profiles[0].agent_id]}
                    memory={memoryByAgent[profiles[0].agent_id] ?? []}
                    contextBundles={contextByAgent[profiles[0].agent_id] ?? []}
                    runs={runs.filter((item) => item.agent_id === profiles[0].agent_id)}
                    onChange={(patch) => updateAgentDraft("agent_a", patch)}
                    onOpenMemoryReview={(revision) =>
                      setReviewState({
                        kind: "memory",
                        label: `${profiles[0].display_name} · ${revision.revision}`,
                        agentId: profiles[0].agent_id,
                        memoryRevision: revision.revision,
                      })
                    }
                  />
                  <AgentConfigCard
                    title={profiles[1].display_name}
                    agent={draftProfile?.agent_b ?? profiles[1]}
                    credentials={credentials}
                    locked={configLocked}
                    typingState={runtimeState.typing[profiles[1].agent_id]}
                    memory={memoryByAgent[profiles[1].agent_id] ?? []}
                    contextBundles={contextByAgent[profiles[1].agent_id] ?? []}
                    runs={runs.filter((item) => item.agent_id === profiles[1].agent_id)}
                    onChange={(patch) => updateAgentDraft("agent_b", patch)}
                    onOpenMemoryReview={(revision) =>
                      setReviewState({
                        kind: "memory",
                        label: `${profiles[1].display_name} · ${revision.revision}`,
                        agentId: profiles[1].agent_id,
                        memoryRevision: revision.revision,
                      })
                    }
                  />
                </>
              ) : (
                <div className="empty-state">
                  <strong>等待实验配置</strong>
                  <span>创建草稿后，这里会显示 Agent A / Agent B 的配置、上下文和运行状态。</span>
                </div>
              )}
            </section>

            <div
              className="drag-divider horizontal"
              role="separator"
              aria-label="次分隔线"
              aria-orientation="horizontal"
              aria-valuemin={28}
              aria-valuemax={68}
              aria-valuenow={Math.round(upperPaneRatio * 100)}
              onPointerDown={(event) => setDragState({ type: "secondary", startY: event.clientY, initialRatio: upperPaneRatio })}
            />

            <section className="panel-card">
              <header className="panel-card-head">
                <div>
                  <h3>系统面板</h3>
                  <p className="subtle">运行护栏、兼容校验、CP/Segment、评分、历史和日志集中展示。</p>
                </div>
                {conversation ? (
                  <button type="button" onClick={() => void handleSaveDraft()} disabled={!draftProfile || configLocked}>
                    保存草稿
                  </button>
                ) : null}
              </header>

            <div className="server-grid">
              <section className="secondary-card">
                <h4>标题与标签</h4>
                <div className="stack-form">
                  <label>
                    <span>目录标题</span>
                    <input
                      aria-label="Metadata Title"
                      value={metadataTitle}
                      onChange={(event) => setMetadataTitle(event.target.value)}
                    />
                  </label>
                  <label>
                    <span>标签</span>
                    <input
                      aria-label="Metadata Tags"
                      value={metadataTags}
                      onChange={(event) => setMetadataTags(event.target.value)}
                      placeholder="例如：验收, scripted"
                    />
                  </label>
                  <button type="button" onClick={() => void handleSaveMetadata()} disabled={!conversation}>
                    保存目录信息
                  </button>
                </div>
              </section>

              <section className="secondary-card">
                <h4>兼容校验与启动确认</h4>
                {validation ? (
                  <div className={validation.ok ? "validation-card ok" : "validation-card warn"}>
                    <strong>{validation.ok ? "校验通过，可以开始实验" : "校验未通过"}</strong>
                    {validation.issues.length > 0 ? (
                      <ul className="issue-list">
                        {validation.issues.map((issue) => (
                          <li key={`${issue.path}-${issue.code}`}>
                            {issue.path}: {issue.message}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <span>当前模板、模型与凭据兼容。</span>
                    )}
                  </div>
                ) : (
                  <div className="empty-inline">先点击“校验配置”查看兼容结果。</div>
                )}
              </section>

              <section className="secondary-card">
                <h4>运行护栏</h4>
                {guardrailsDraft ? (
                  <div className="stack-form">
                    <label>
                      <span>Pause</span>
                      <input
                        aria-label="Pause Guardrail"
                        type="checkbox"
                        checked={guardrailsDraft.pause}
                        disabled={!conversation || !["running", "paused"].includes(conversation.status)}
                        onChange={(event) =>
                          setGuardrailsDraft({ ...guardrailsDraft, pause: event.target.checked })
                        }
                      />
                    </label>
                    <label>
                      <span>Stop</span>
                      <input
                        aria-label="Stop Guardrail"
                        type="checkbox"
                        checked={guardrailsDraft.stop}
                        disabled={!conversation || !["running", "paused"].includes(conversation.status)}
                        onChange={(event) =>
                          setGuardrailsDraft({ ...guardrailsDraft, stop: event.target.checked })
                        }
                      />
                    </label>
                    <label>
                      <span>Log Level</span>
                      <input
                        aria-label="Log Level"
                        value={guardrailsDraft.log_level}
                        disabled={!conversation || !["running", "paused"].includes(conversation.status)}
                        onChange={(event) =>
                          setGuardrailsDraft({ ...guardrailsDraft, log_level: event.target.value })
                        }
                      />
                    </label>
                    <button
                      type="button"
                      onClick={() => void handleGuardrailSave()}
                      disabled={!conversation || !["running", "paused"].includes(conversation.status)}
                    >
                      保存运行护栏
                    </button>
                  </div>
                ) : null}
              </section>

              <section className="secondary-card">
                <h4>Segment / CP</h4>
                <div className="timeline">
                  {cpRevisions.map((revision) => (
                    <button
                      key={revision.projection_revision}
                      type="button"
                      className="timeline-item"
                      onClick={() =>
                        setReviewState({
                          kind: "cp",
                          label: `CP revision ${revision.projection_revision}`,
                          cpRevision: revision.projection_revision,
                        })
                      }
                    >
                      <strong>rev {revision.projection_revision}</strong>
                      <span>{revision.snapshot.segments.at(-1)?.title ?? "无片段"}</span>
                      <span>{revision.snapshot.segments.at(-1)?.summary ?? "等待公共消息"}</span>
                    </button>
                  ))}
                </div>
                {latestCp ? <RawJsonCard title="当前 CP 快照" value={latestCp.snapshot} /> : null}
              </section>

              <section className="secondary-card">
                <h4>人工评分与导出</h4>
                {automaticMetrics ? (
                  <dl className="metric-grid">
                    <div>
                      <dt>自动指标时间</dt>
                      <dd>{formatTime(automaticMetrics.computed_at)}</dd>
                    </div>
                    <div>
                      <dt>总 Token</dt>
                      <dd>{costs?.total_tokens ?? 0}</dd>
                    </div>
                    <div>
                      <dt>费用估算</dt>
                      <dd>${costs?.total_cost_usd.toFixed(6) ?? "0.000000"}</dd>
                    </div>
                  </dl>
                ) : null}
                {manualScore ? (
                  <div className="stack-form">
                    {manualScore.scores.map((item, index) => (
                      <div key={`${item.criterion}-${index}`} className="manual-score-row">
                        <label>
                          <span>{item.criterion}</span>
                          <input
                            aria-label={`${item.criterion} score`}
                            type="number"
                            min={0}
                            max={5}
                            step={0.5}
                            value={item.score ?? ""}
                            onChange={(event) =>
                              setManualScore({
                                ...manualScore,
                                scores: manualScore.scores.map((scoreItem, scoreIndex) =>
                                  scoreIndex === index
                                    ? {
                                        ...scoreItem,
                                        score: event.target.value === "" ? null : Number(event.target.value),
                                      }
                                    : scoreItem,
                                ),
                              })
                            }
                          />
                        </label>
                        <label>
                          <span>备注</span>
                          <input
                            aria-label={`${item.criterion} note`}
                            value={item.note}
                            onChange={(event) =>
                              setManualScore({
                                ...manualScore,
                                scores: manualScore.scores.map((scoreItem, scoreIndex) =>
                                  scoreIndex === index ? { ...scoreItem, note: event.target.value } : scoreItem,
                                ),
                              })
                            }
                          />
                        </label>
                      </div>
                    ))}
                    <label>
                      <span>总体备注</span>
                      <input
                        aria-label="Manual Score Overall Note"
                        value={manualScore.overall_note}
                        onChange={(event) =>
                          setManualScore({ ...manualScore, overall_note: event.target.value })
                        }
                      />
                    </label>
                    <div className="row-actions">
                      <button type="button" onClick={() => void handleManualScoreSave()}>
                        保存人工评分
                      </button>
                      <button type="button" onClick={() => void handleCreateExport()}>
                        生成分析 ZIP
                      </button>
                    </div>
                    {exportJob ? (
                      <div className="export-status">
                        <strong>导出状态</strong>
                        <span>{exportJob.status}</span>
                        {exportJob.download_url ? (
                          <a href={`${apiBase.replace(/\/$/, "")}${exportJob.download_url}`}>下载 ZIP</a>
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </section>

              <section className="secondary-card">
                <h4>历史实验</h4>
                <div className="conversation-list">
                  {historyEntries.map((item) => (
                    <button
                      key={item.conversation_id}
                      type="button"
                      className={item.conversation_id === selectedHistoryId ? "conversation-item active" : "conversation-item"}
                      onClick={() => setSelectedHistoryId(item.conversation_id)}
                    >
                      <strong>{item.title ?? item.conversation_id}</strong>
                      <span>{item.adapter_known ? "当前可识别" : "raw fallback"}</span>
                      <span>{formatTime(item.ended_at ?? item.created_at)}</span>
                    </button>
                  ))}
                </div>
                {historyDetail ? (
                  <>
                    <div className="history-summary">
                      <strong>{historyDetail.entry.title ?? historyDetail.entry.conversation_id}</strong>
                      <span>{historyDetail.entry.adapter_known ? "兼容" : "raw fallback"}</span>
                    </div>
                    <RawJsonCard title="历史档案 / Raw Fallback" value={historyDetail.raw_manifest} />
                  </>
                ) : null}
              </section>

              <section className="secondary-card">
                <h4>系统日志与详情</h4>
                <div className="log-list">
                  {selectedServerLogs.map((entry) => (
                    <div key={entry.id} className="log-item">
                      <strong>{entry.level}</strong>
                      <span>{entry.message}</span>
                      <span>{formatTime(entry.timestamp)}</span>
                    </div>
                  ))}
                </div>
                <RawJsonCard title="自动指标" value={automaticMetrics} />
                <RawJsonCard title="Server Logs" value={logs} />
              </section>
            </div>
          </section>
          </div>
        </section>
      </main>
      {wizardOpen ? (
        <div className="wizard-backdrop" role="dialog" aria-modal="true" aria-label="新建实验向导">
          <section className="wizard-modal">
            <header className="panel-card-head">
              <div>
                <p className="eyebrow">新建实验向导</p>
                <h2>{wizardStep.title}</h2>
                <p className="subtle">{wizardStep.description}</p>
              </div>
              <button type="button" onClick={() => setWizardOpen(false)}>关闭</button>
            </header>
            <div className="wizard-steps">
              {WIZARD_STEPS.map((step, index) => (
                <button
                  key={step.id}
                  type="button"
                  className={index === wizardStepIndex ? "wizard-step active" : "wizard-step"}
                  onClick={() => setWizardStepIndex(index)}
                >
                  <strong>{step.title}</strong>
                  <span>{step.description}</span>
                </button>
              ))}
            </div>
            {wizardProfile ? (
              <div className="wizard-body">
                {wizardStep.id === "template" ? (
                  <div className="wizard-grid">
                    <label>
                      <span>模板</span>
                      <select aria-label="Wizard Template Select" value={draftTemplateId} onChange={(event) => setDraftTemplateId(event.target.value)}>
                        {templates.map((template) => (
                          <option key={template.id} value={template.id}>{template.title}</option>
                        ))}
                      </select>
                    </label>
                    <label>
                      <span>实验标题</span>
                      <input aria-label="Wizard Draft Title" value={draftTitle} onChange={(event) => setDraftTitle(event.target.value)} />
                    </label>
                    <label>
                      <span>策略包说明</span>
                      <textarea aria-label="Wizard Template Description" value={templates.find((item) => item.id === draftTemplateId)?.description ?? ""} readOnly />
                    </label>
                  </div>
                ) : null}
                {wizardStep.id === "roles" ? (
                  <div className="wizard-grid">
                    <label>
                      <span>Agent A 名称</span>
                      <input aria-label="Wizard Agent A Name" value={wizardProfile.agent_a.display_name} onChange={(event) => updateWizardProfile((current) => ({ ...current, agent_a: { ...current.agent_a, display_name: event.target.value } }))} />
                    </label>
                    <label>
                      <span>Agent B 名称</span>
                      <input aria-label="Wizard Agent B Name" value={wizardProfile.agent_b.display_name} onChange={(event) => updateWizardProfile((current) => ({ ...current, agent_b: { ...current.agent_b, display_name: event.target.value } }))} />
                    </label>
                    <label>
                      <span>Agent A Persona</span>
                      <textarea aria-label="Wizard Agent A Persona" value={wizardProfile.agent_a.persona_prompt} onChange={(event) => updateWizardProfile((current) => ({ ...current, agent_a: { ...current.agent_a, persona_prompt: event.target.value } }))} />
                    </label>
                    <label>
                      <span>Agent B Persona</span>
                      <textarea aria-label="Wizard Agent B Persona" value={wizardProfile.agent_b.persona_prompt} onChange={(event) => updateWizardProfile((current) => ({ ...current, agent_b: { ...current.agent_b, persona_prompt: event.target.value } }))} />
                    </label>
                  </div>
                ) : null}
                {wizardStep.id === "models" ? (
                  <div className="wizard-grid">
                    <label>
                      <span>Agent A Provider</span>
                      <input aria-label="Wizard Agent A Provider" value={wizardProfile.agent_a.model.provider} onChange={(event) => updateWizardProfile((current) => ({ ...current, agent_a: { ...current.agent_a, model: { ...current.agent_a.model, provider: event.target.value } } }))} />
                    </label>
                    <label>
                      <span>Agent A 模型</span>
                      <input aria-label="Wizard Agent A Model" value={wizardProfile.agent_a.model.model} onChange={(event) => updateWizardProfile((current) => ({ ...current, agent_a: { ...current.agent_a, model: { ...current.agent_a.model, model: event.target.value } } }))} />
                    </label>
                    <label>
                      <span>Agent A Credential</span>
                      <select aria-label="Wizard Agent A Credential" value={wizardProfile.agent_a.model.credential_ref ?? ""} onChange={(event) => updateWizardProfile((current) => ({ ...current, agent_a: { ...current.agent_a, model: { ...current.agent_a.model, credential_ref: event.target.value || null } } }))}>
                        <option value="">Scripted / 无需凭据</option>
                        {credentials.map((credential) => <option key={credential.credential_ref} value={credential.credential_ref}>{credential.label}</option>)}
                      </select>
                    </label>
                    <label>
                      <span>Agent B Provider</span>
                      <input aria-label="Wizard Agent B Provider" value={wizardProfile.agent_b.model.provider} onChange={(event) => updateWizardProfile((current) => ({ ...current, agent_b: { ...current.agent_b, model: { ...current.agent_b.model, provider: event.target.value } } }))} />
                    </label>
                    <label>
                      <span>Agent B 模型</span>
                      <input aria-label="Wizard Agent B Model" value={wizardProfile.agent_b.model.model} onChange={(event) => updateWizardProfile((current) => ({ ...current, agent_b: { ...current.agent_b, model: { ...current.agent_b.model, model: event.target.value } } }))} />
                    </label>
                    <label>
                      <span>Agent B Credential</span>
                      <select aria-label="Wizard Agent B Credential" value={wizardProfile.agent_b.model.credential_ref ?? ""} onChange={(event) => updateWizardProfile((current) => ({ ...current, agent_b: { ...current.agent_b, model: { ...current.agent_b.model, credential_ref: event.target.value || null } } }))}>
                        <option value="">Scripted / 无需凭据</option>
                        {credentials.map((credential) => <option key={credential.credential_ref} value={credential.credential_ref}>{credential.label}</option>)}
                      </select>
                    </label>
                  </div>
                ) : null}
                {wizardStep.id === "attention" ? (
                  <div className="wizard-grid">
                    <label>
                      <span>Agent A Attention Prior</span>
                      <textarea aria-label="Wizard Agent A Attention" value={wizardProfile.agent_a.attention_prior} onChange={(event) => updateWizardProfile((current) => ({ ...current, agent_a: { ...current.agent_a, attention_prior: event.target.value } }))} />
                    </label>
                    <label>
                      <span>Agent B Attention Prior</span>
                      <textarea aria-label="Wizard Agent B Attention" value={wizardProfile.agent_b.attention_prior} onChange={(event) => updateWizardProfile((current) => ({ ...current, agent_b: { ...current.agent_b, attention_prior: event.target.value } }))} />
                    </label>
                    <label>
                      <span>Agent A 阈值</span>
                      <input aria-label="Wizard Agent A Threshold" type="number" min={0} max={1} step={0.05} value={wizardProfile.agent_a.response_threshold} onChange={(event) => updateWizardProfile((current) => ({ ...current, agent_a: { ...current.agent_a, response_threshold: Number(event.target.value) || 0 } }))} />
                    </label>
                    <label>
                      <span>Agent B 阈值</span>
                      <input aria-label="Wizard Agent B Threshold" type="number" min={0} max={1} step={0.05} value={wizardProfile.agent_b.response_threshold} onChange={(event) => updateWizardProfile((current) => ({ ...current, agent_b: { ...current.agent_b, response_threshold: Number(event.target.value) || 0 } }))} />
                    </label>
                  </div>
                ) : null}
                {wizardStep.id === "params" ? (
                  <div className="wizard-grid">
                    <label>
                      <span>Prompt 版本</span>
                      <input aria-label="Wizard Prompt Version" value={wizardProfile.prompt_version} onChange={(event) => updateWizardProfile((current) => ({ ...current, prompt_version: event.target.value }))} />
                    </label>
                    <label>
                      <span>最大 LLM 调用</span>
                      <input aria-label="Wizard Max Llm Calls" type="number" value={wizardGuardrails.max_llm_calls} onChange={(event) => setWizardGuardrails((current) => ({ ...current, max_llm_calls: Number(event.target.value) || 0 }))} />
                    </label>
                    <label>
                      <span>最大 Token</span>
                      <input aria-label="Wizard Max Total Tokens" type="number" value={wizardGuardrails.max_total_tokens} onChange={(event) => setWizardGuardrails((current) => ({ ...current, max_total_tokens: Number(event.target.value) || 0 }))} />
                    </label>
                    <label>
                      <span>最大自动重试</span>
                      <input aria-label="Wizard Max Auto Retries" type="number" value={wizardGuardrails.max_auto_retries} onChange={(event) => setWizardGuardrails((current) => ({ ...current, max_auto_retries: Number(event.target.value) || 0 }))} />
                    </label>
                  </div>
                ) : null}
                {wizardStep.id === "projection" ? (
                  <div className="wizard-grid">
                    <label>
                      <span>Projection 模块</span>
                      <input aria-label="Wizard Projection Module" value={wizardProfile.modules.projection?.module_id ?? ""} onChange={(event) => updateWizardProfile((current) => ({ ...current, modules: { ...current.modules, projection: { ...(current.modules.projection ?? { config: {} }), module_id: event.target.value, config: current.modules.projection?.config ?? {} } } }))} />
                    </label>
                    <label>
                      <span>消息序列模块</span>
                      <input aria-label="Wizard Message Sequence Module" value={wizardProfile.modules.message_sequence?.module_id ?? ""} onChange={(event) => updateWizardProfile((current) => ({ ...current, modules: { ...current.modules, message_sequence: { ...(current.modules.message_sequence ?? { config: {} }), module_id: event.target.value, config: current.modules.message_sequence?.config ?? {} } } }))} />
                    </label>
                    <label>
                      <span>模板 ID</span>
                      <input aria-label="Wizard Template Id" value={wizardProfile.template_id} onChange={(event) => updateWizardProfile((current) => ({ ...current, template_id: event.target.value }))} />
                    </label>
                  </div>
                ) : null}
                {wizardStep.id === "guardrails" ? (
                  <div className="wizard-grid">
                    <label>
                      <span>Pause</span>
                      <input aria-label="Wizard Pause Guardrail" type="checkbox" checked={wizardGuardrails.pause} onChange={(event) => setWizardGuardrails((current) => ({ ...current, pause: event.target.checked }))} />
                    </label>
                    <label>
                      <span>Stop</span>
                      <input aria-label="Wizard Stop Guardrail" type="checkbox" checked={wizardGuardrails.stop} onChange={(event) => setWizardGuardrails((current) => ({ ...current, stop: event.target.checked }))} />
                    </label>
                    <label>
                      <span>Log Level</span>
                      <input aria-label="Wizard Log Level" value={wizardGuardrails.log_level} onChange={(event) => setWizardGuardrails((current) => ({ ...current, log_level: event.target.value }))} />
                    </label>
                    <label>
                      <span>Worker Restart Limit</span>
                      <input aria-label="Wizard Worker Restart Limit" type="number" value={wizardGuardrails.worker_restart_limit} onChange={(event) => setWizardGuardrails((current) => ({ ...current, worker_restart_limit: Number(event.target.value) || 0 }))} />
                    </label>
                  </div>
                ) : null}
                {wizardStep.id === "validation" ? (
                  <div className="wizard-grid">
                    <div className={wizardPreflightIssues.length === 0 ? "validation-card ok" : "validation-card warn"}>
                      <strong>{wizardPreflightIssues.length === 0 ? "前端预检通过" : "前端预检发现问题"}</strong>
                      {wizardPreflightIssues.length > 0 ? (
                        <ul className="issue-list">{wizardPreflightIssues.map((issue) => <li key={issue}>{issue}</li>)}</ul>
                      ) : (
                        <span>本地字段已满足创建草稿的最小条件。</span>
                      )}
                    </div>
                    {wizardValidation ? (
                      <div className={wizardValidation.ok ? "validation-card ok" : "validation-card warn"}>
                        <strong>{wizardValidation.ok ? "服务端校验通过" : "服务端校验未通过"}</strong>
                        {wizardValidation.issues.length > 0 ? (
                          <ul className="issue-list">
                            {wizardValidation.issues.map((issue) => <li key={`${issue.path}-${issue.code}`}>{issue.path}: {issue.message}</li>)}
                          </ul>
                        ) : null}
                      </div>
                    ) : (
                      <div className="empty-inline">创建草稿后，这里会显示服务端兼容校验结果。</div>
                    )}
                  </div>
                ) : null}
                {wizardStep.id === "confirm" ? (
                  <div className="wizard-grid">
                    <div className="secondary-card">
                      <h4>配置 Diff</h4>
                      <ul className="issue-list">{wizardDiffLines.map((line) => <li key={line}>{line}</li>)}</ul>
                    </div>
                    <div className="secondary-card">
                      <h4>启动确认</h4>
                      <p className="subtle">将创建草稿并立即执行一次服务端兼容校验；成功后回到主界面继续点击“开始实验”。</p>
                      <dl className="metric-grid">
                        <div><dt>模板</dt><dd>{templates.find((item) => item.id === draftTemplateId)?.title ?? "-"}</dd></div>
                        <div><dt>Guardrails</dt><dd>{wizardGuardrails.max_llm_calls} 次 / {wizardGuardrails.max_total_tokens} tokens</dd></div>
                        <div><dt>凭据</dt><dd>{credentials.length} 条已配置</dd></div>
                      </dl>
                    </div>
                  </div>
                ) : null}
              </div>
            ) : null}
            <footer className="wizard-footer">
              <button type="button" onClick={() => goWizardStep(-1)} disabled={wizardStepIndex === 0 || wizardSubmitting}>上一步</button>
              {wizardStepIndex < WIZARD_STEPS.length - 1 ? (
                <button type="button" onClick={() => goWizardStep(1)} disabled={wizardSubmitting}>下一步</button>
              ) : (
                <button type="button" onClick={() => void handleWizardCreateDraft()} disabled={wizardSubmitting}>
                  {wizardSubmitting ? "创建中…" : "创建草稿并校验"}
                </button>
              )}
            </footer>
          </section>
        </div>
      ) : null}
    </div>
  );
}

function AgentConfigCard({
  title,
  agent,
  credentials,
  locked,
  typingState,
  memory,
  contextBundles,
  runs,
  onChange,
  onOpenMemoryReview,
}: {
  title: string;
  agent: AgentProfile;
  credentials: CredentialRead[];
  locked: boolean;
  typingState: { active: boolean; runId: string | null; reason?: string } | undefined;
  memory: MemoryRevision[];
  contextBundles: ContextBundle[];
  runs: AgentRun[];
  onChange: (patch: Partial<AgentProfile>) => void;
  onOpenMemoryReview: (revision: MemoryRevision) => void;
}): JSX.Element {
  return (
    <section className="panel-card">
      <header className="panel-card-head">
        <div>
          <h3>{title}</h3>
          <p className="subtle">{typingState?.active ? `typing · ${typingState.reason ?? "running"}` : "idle"}</p>
        </div>
      </header>
      <div className="stack-form">
        <label>
          <span>显示名</span>
          <input
            aria-label={`${title} Display Name`}
            value={agent.display_name}
            disabled={locked}
            onChange={(event) => onChange({ display_name: event.target.value })}
          />
        </label>
        <label>
          <span>Persona</span>
          <textarea
            aria-label={`${title} Persona`}
            value={agent.persona_prompt}
            disabled={locked}
            onChange={(event) => onChange({ persona_prompt: event.target.value })}
          />
        </label>
        <label>
          <span>Attention Prior</span>
          <textarea
            aria-label={`${title} Attention Prior`}
            value={agent.attention_prior}
            disabled={locked}
            onChange={(event) => onChange({ attention_prior: event.target.value })}
          />
        </label>
        <label>
          <span>模型 Provider</span>
          <input
            aria-label={`${title} Model Provider`}
            value={agent.model.provider}
            disabled={locked}
            onChange={(event) =>
              onChange({ model: { ...agent.model, provider: event.target.value } })
            }
          />
        </label>
        <label>
          <span>模型名</span>
          <input
            aria-label={`${title} Model Name`}
            value={agent.model.model}
            disabled={locked}
            onChange={(event) => onChange({ model: { ...agent.model, model: event.target.value } })}
          />
        </label>
        <label>
          <span>Credential Ref</span>
          <select
            aria-label={`${title} Credential Ref`}
            value={agent.model.credential_ref ?? ""}
            disabled={locked}
            onChange={(event) =>
              onChange({
                model: {
                  ...agent.model,
                  credential_ref: event.target.value === "" ? null : event.target.value,
                },
              })
            }
          >
            <option value="">Scripted / 无需凭据</option>
            {credentials.map((credential) => (
              <option key={credential.credential_ref} value={credential.credential_ref}>
                {credential.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>响应阈值</span>
          <input
            aria-label={`${title} Response Threshold`}
            type="number"
            min={0}
            max={1}
            step={0.05}
            value={agent.response_threshold}
            disabled={locked}
            onChange={(event) =>
              onChange({ response_threshold: Number(event.target.value) || 0 })
            }
          />
        </label>
      </div>
      <div className="timeline">
        {runs.slice(-4).map((run) => (
          <button key={run.run_id} type="button" className="timeline-item">
            <strong>{run.status}</strong>
            <span>{run.phase}</span>
            <span>{formatTime(run.started_at)}</span>
          </button>
        ))}
      </div>
      <div className="timeline">
        {memory.slice(-3).map((revision) => (
          <button
            key={revision.revision}
            type="button"
            className="timeline-item"
            onClick={() => onOpenMemoryReview(revision)}
          >
            <strong>{revision.revision}</strong>
            <span>{formatTime(revision.committed_at)}</span>
          </button>
        ))}
      </div>
      <RawJsonCard title={`${title} Memory / Context`} value={{ memory, contextBundles }} />
    </section>
  );
}
