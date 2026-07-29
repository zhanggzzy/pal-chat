import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import {
  createAnalysisExport,
  getAnalysisExport,
  loadHistoryDetail,
  loadWorkbench,
  requestJson,
  saveManualScore,
} from "./api";
import { applySocketPayload, emptyRuntimeViewState, inferRunForMessage, upsertMessage } from "./state";
import "./styles.css";
import type {
  AnalysisExportJob,
  AgentProfile,
  AgentRun,
  AutomaticMetrics,
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
  OutboxEvent,
  ReviewState,
  RuntimeGuardrails,
  SessionSnapshot,
} from "./types";

type InspectorView = "server" | "agent-a" | "agent-b";

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
  if (typeof window !== "undefined" && window.location.origin.startsWith("http")) {
    return window.location.origin === "http://localhost:5173"
      ? "http://127.0.0.1:8000"
      : window.location.origin;
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

function agentProfiles(conversation: ConversationRead | null): [AgentProfile, AgentProfile] | null {
  if (!conversation) {
    return null;
  }
  const profile = conversation.locked_profile ?? conversation.draft_profile;
  return [profile.agent_a, profile.agent_b];
}

function RawJsonCard({
  title,
  value,
}: {
  title: string;
  value: unknown;
}): JSX.Element {
  return (
    <section className="panel-card">
      <header className="panel-card-head">
        <h4>{title}</h4>
      </header>
      <pre className="json-fallback">{asJson(value)}</pre>
    </section>
  );
}

export function App(): JSX.Element {
  const [apiBase, setApiBase] = useState(fallbackUrl());
  const [conversations, setConversations] = useState<ConversationRead[]>([]);
  const [conversation, setConversation] = useState<ConversationRead | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [cpRevisions, setCpRevisions] = useState<CpRevision[]>([]);
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [costs, setCosts] = useState<CostBreakdown | null>(null);
  const [memoryByAgent, setMemoryByAgent] = useState<Record<string, MemoryRevision[]>>({});
  const [contextByAgent, setContextByAgent] = useState<Record<string, ContextBundle[]>>({});
  const [attemptsByAgent, setAttemptsByAgent] = useState<Record<string, AgentRun["attempts"]>>({});
  const [automaticMetrics, setAutomaticMetrics] = useState<AutomaticMetrics | null>(null);
  const [manualScore, setManualScore] = useState<ManualScore | null>(null);
  const [historyEntries, setHistoryEntries] = useState<HistoryEntry[]>([]);
  const [selectedHistoryId, setSelectedHistoryId] = useState<string>("");
  const [historyDetail, setHistoryDetail] = useState<HistoryDetail | null>(null);
  const [exportJob, setExportJob] = useState<AnalysisExportJob | null>(null);
  const [selectedConversationId, setSelectedConversationId] = useState<string>("");
  const [selectedMessageId, setSelectedMessageId] = useState<string>("");
  const [selectedRunId, setSelectedRunId] = useState<string>("");
  const [selectedInspector, setSelectedInspector] = useState<InspectorView>("server");
  const [reviewState, setReviewState] = useState<ReviewState | null>(null);
  const [guardrailsDraft, setGuardrailsDraft] = useState<RuntimeGuardrails | null>(null);
  const [composer, setComposer] = useState("");
  const [statusText, setStatusText] = useState("初始化中");
  const [errorText, setErrorText] = useState<string | null>(null);
  const [wsBanner, setWsBanner] = useState<string | null>(null);
  const [runtimeState, setRuntimeState] = useState(emptyRuntimeViewState());
  const [needsReviewResync, setNeedsReviewResync] = useState(false);
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);

  const selectedRun = useMemo(
    () => runs.find((item) => item.run_id === selectedRunId) ?? null,
    [runs, selectedRunId],
  );
  const selectedMessage = useMemo(
    () => messages.find((item) => item.message_id === selectedMessageId) ?? null,
    [messages, selectedMessageId],
  );
  const profiles = agentProfiles(conversation);

  async function refreshConversation(conversationId: string): Promise<void> {
    setStatusText("同步工作台");
    const payload = await loadWorkbench(apiBase, conversationId);
    setConversations(payload.conversations);
    setConversation(payload.detail);
    setMessages(payload.messages);
    setCpRevisions(payload.cpRevisions);
    setRuns(payload.runs);
    setLogs(payload.logs);
    setCosts(payload.costs);
    setMemoryByAgent(payload.memoryByAgent);
    setContextByAgent(payload.contextByAgent);
    setAttemptsByAgent(payload.attemptsByAgent);
    setAutomaticMetrics(payload.automaticMetrics);
    setManualScore(normalizeManualScore(payload.manualScore));
    setHistoryEntries(payload.history);
    if (!selectedHistoryId && payload.history.length > 0) {
      setSelectedHistoryId(payload.history[0].conversation_id);
    }
    setGuardrailsDraft(payload.detail.guardrails);
    setRuntimeState({
      ...emptyRuntimeViewState(),
      messages: payload.messages,
      cpRevisions: payload.cpRevisions,
      runs: payload.runs,
      latestSeq: payload.messages.at(-1)?.conversation_seq ?? 0,
      latestCpRevision: payload.cpRevisions.at(-1)?.projection_revision ?? 0,
      guardrails: payload.detail.guardrails,
      typing: {},
    });
    setStatusText("已同步");
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        setErrorText(null);
        const list = await requestJson<{ items: ConversationRead[] }>(apiBase, "/api/v1/conversations");
        if (cancelled) {
          return;
        }
        setConversations(list.items);
        const target =
          list.items.find((item) => item.status === "running") ??
          list.items.find((item) => item.status === "ended") ??
          list.items[0] ??
          null;
        if (target) {
          setSelectedConversationId(target.id);
        }
      } catch (error) {
        if (!cancelled) {
          setErrorText(error instanceof Error ? error.message : "无法读取会话列表");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [apiBase]);

  useEffect(() => {
    if (!selectedConversationId) {
      return;
    }
    void refreshConversation(selectedConversationId).catch((error: unknown) => {
      setErrorText(error instanceof Error ? error.message : "同步失败");
    });
  }, [apiBase, selectedConversationId]);

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
    if (!selectedConversationId) {
      return;
    }
    if (socketRef.current) {
      socketRef.current.close();
    }
    const url = new URL(`/ws/v1/conversations/${selectedConversationId}`, apiBase);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("after_seq", String(runtimeState.latestSeq));
    const socket = new WebSocket(url);
    socketRef.current = socket;
    socket.addEventListener("open", () => {
      setWsBanner(null);
      void refreshConversation(selectedConversationId).catch(() => undefined);
    });
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
        setSelectedConversationId((current) => current);
      }, 5000);
    });
    return () => {
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
      }
      socket.close();
    };
  }, [apiBase, reviewState, runtimeState.latestSeq, selectedConversationId]);

  useEffect(() => {
    setMessages(runtimeState.messages);
  }, [runtimeState.messages]);

  useEffect(() => {
    setCpRevisions(runtimeState.cpRevisions);
  }, [runtimeState.cpRevisions]);

  useEffect(() => {
    setRuns(runtimeState.runs);
  }, [runtimeState.runs]);

  useEffect(() => {
    if (runtimeState.guardrails) {
      setGuardrailsDraft(runtimeState.guardrails);
    }
  }, [runtimeState.guardrails]);

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
      mentions: [],
      primary_reply_to: null,
      responds_to: [],
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
    try {
      const payload = await requestJson<{ message: Message }>(
        apiBase,
        `/api/v1/conversations/${conversation.id}/messages`,
        {
          method: "POST",
          body: JSON.stringify({
            client_message_id: clientMessageId,
            content_markdown: optimistic.content_markdown,
            mentions: [],
            responds_to: [],
            primary_reply_to: null,
          }),
        },
      );
      setMessages((current) => upsertMessage(current, { ...payload.message, ui_status: "sent" }));
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

  async function handleGuardrailSave(): Promise<void> {
    if (!conversation || !guardrailsDraft) {
      return;
    }
    if (reviewState !== null || conversation.status !== "running") {
      setErrorText("回看或只读模式下禁止写请求。");
      return;
    }
    try {
      const updated = await requestJson<ConversationRead>(
        apiBase,
        `/api/v1/conversations/${conversation.id}/guardrails`,
        {
          method: "PATCH",
          body: JSON.stringify(guardrailsDraft),
        },
      );
      setConversation(updated);
      setGuardrailsDraft(updated.guardrails);
      setStatusText("Guardrails 已更新");
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "Guardrails 更新失败");
    }
  }

  async function handleManualScoreSave(): Promise<void> {
    if (!conversation || !manualScore) {
      return;
    }
    if (reviewState !== null) {
      setErrorText("回看模式下禁止写请求。");
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
      setStatusText("分析 ZIP 已排队");
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "分析导出创建失败");
    }
  }

  function exitReview(): void {
    setReviewState(null);
    if (selectedConversationId && needsReviewResync) {
      setNeedsReviewResync(false);
      void refreshConversation(selectedConversationId).catch(() => undefined);
    }
  }

  function displayMessages(): Message[] {
    if (reviewState?.kind !== "cp" || reviewState.cpRevision === undefined) {
      return messages;
    }
    const cpRevision = reviewState.cpRevision;
    return messages.filter((item) => item.cp_revision <= cpRevision);
  }

  function displayedMemory(agentId: string): MemoryRevision[] {
    const revisions = memoryByAgent[agentId] ?? [];
    if (reviewState?.kind === "memory" && reviewState.agentId === agentId && reviewState.memoryRevision) {
      return revisions.filter((item) => item.revision === reviewState.memoryRevision);
    }
    return revisions;
  }

  function handleMessageSelect(message: Message): void {
    setSelectedMessageId(message.message_id);
    const run = inferRunForMessage(runs, message);
    if (run) {
      setSelectedRunId(run.run_id);
      setSelectedInspector(run.agent_id === profiles?.[0].agent_id ? "agent-a" : "agent-b");
    }
  }

  const visibleMessages = displayMessages();
  const liveReadOnly = reviewState !== null || conversation?.status !== "running";
  const latestCp = cpRevisions.at(-1) ?? null;
  const currentTyping = runtimeState.typing;

  return (
    <div className="shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">pal-chat / phase 6</p>
          <h1>监控工作台</h1>
        </div>
        <label className="endpoint">
          <span>API Base</span>
          <input aria-label="API Base" value={apiBase} onChange={(event) => setApiBase(event.target.value)} />
        </label>
        <div className="status-strip" aria-live="polite">
          <span>{statusText}</span>
          <span>{conversation ? `${conversation.status} · seq ${runtimeState.latestSeq}` : "未选择会话"}</span>
          <span>CP rev {runtimeState.latestCpRevision}</span>
        </div>
      </header>

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

      <main className="workbench">
        <aside className="sidebar">
          <section className="panel-card">
            <header className="panel-card-head">
              <h2>实验列表</h2>
            </header>
            <div className="conversation-list" role="list">
              {conversations.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={item.id === selectedConversationId ? "conversation-item active" : "conversation-item"}
                  onClick={() => setSelectedConversationId(item.id)}
                >
                  <strong>{item.title}</strong>
                  <span>{item.status}</span>
                  <span>{formatTime(item.updated_at)}</span>
                </button>
              ))}
            </div>
          </section>

          <section className="panel-card">
            <header className="panel-card-head">
              <h2>历史目录</h2>
            </header>
            <div className="conversation-list" role="list">
              {historyEntries.map((item) => (
                <button
                  key={item.conversation_id}
                  type="button"
                  className={
                    item.conversation_id === selectedHistoryId ? "conversation-item active" : "conversation-item"
                  }
                  onClick={() => setSelectedHistoryId(item.conversation_id)}
                >
                  <strong>{item.title ?? item.conversation_id}</strong>
                  <span>{item.adapter_known ? item.adapter_module_id : "raw fallback"}</span>
                  <span>{formatTime(item.ended_at ?? item.created_at)}</span>
                </button>
              ))}
            </div>
          </section>

          <section className="panel-card">
            <header className="panel-card-head">
              <h2>费用与预算</h2>
            </header>
            <dl className="metric-grid">
              <div>
                <dt>Total Tokens</dt>
                <dd>{costs?.total_tokens ?? 0}</dd>
              </div>
              <div>
                <dt>费用估算</dt>
                <dd>${costs?.total_cost_usd.toFixed(6) ?? "0.000000"}</dd>
                <small>估算/解释性数据，非账单</small>
              </div>
            </dl>
            <pre className="json-fallback">{asJson(costs)}</pre>
          </section>
        </aside>

        <section className="chat-pane">
          <div className="pane-head">
            <div>
              <p className="eyebrow">Chat</p>
              <h2>{conversation?.title ?? "未连接实验"}</h2>
            </div>
            <span className={liveReadOnly ? "mode-pill readonly" : "mode-pill"}>
              {liveReadOnly ? "只读" : "live"}
            </span>
          </div>
          <div className="message-log" role="log" aria-live="polite">
            {visibleMessages.map((message) => (
              <button
                key={message.message_id}
                type="button"
                className={message.message_id === selectedMessageId ? "message-card active" : "message-card"}
                onClick={() => handleMessageSelect(message)}
              >
                <div className="message-head">
                  <span>#{message.conversation_seq}</span>
                  <span>{message.sender_id}</span>
                  <span>{message.ui_status ?? "sent"}</span>
                </div>
                <div className="message-body">{message.content_markdown}</div>
                <div className="message-meta">
                  <span>CP {message.cp_revision}</span>
                  <span>{message.causal_episode_id ?? "no-episode"}</span>
                  <span>hop {message.agent_hop}</span>
                  <span>{formatTime(message.committed_at)}</span>
                </div>
              </button>
            ))}
          </div>
          <form className="composer" onSubmit={(event) => void handleSubmitMessage(event)}>
            <textarea
              aria-label="Public Message"
              value={composer}
              onChange={(event) => setComposer(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  setComposer("");
                }
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void handleSubmitMessage(event as unknown as FormEvent<HTMLFormElement>);
                }
              }}
              disabled={liveReadOnly}
              placeholder={liveReadOnly ? "回看或结束态只读" : "输入公共消息，Enter 发送，Shift+Enter 换行"}
            />
            <button type="submit" disabled={liveReadOnly}>
              发送
            </button>
          </form>
        </section>

        <section className="inspectors">
          <div className="tabs" role="tablist" aria-label="Inspector Tabs">
            <button role="tab" aria-selected={selectedInspector === "agent-a"} onClick={() => setSelectedInspector("agent-a")}>
              A 私有视图
            </button>
            <button role="tab" aria-selected={selectedInspector === "agent-b"} onClick={() => setSelectedInspector("agent-b")}>
              B 私有视图
            </button>
            <button role="tab" aria-selected={selectedInspector === "server"} onClick={() => setSelectedInspector("server")}>
              Server 公共视图
            </button>
          </div>

          {selectedInspector === "server" ? (
            <div className="inspector-stack" role="tabpanel">
              <section className="panel-card">
                <header className="panel-card-head">
                  <h3>CP / Segments</h3>
                </header>
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
                      <span>{revision.snapshot.segments.at(-1)?.summary ?? "无 segment"}</span>
                    </button>
                  ))}
                </div>
                {latestCp ? <RawJsonCard title="当前 CP 快照" value={latestCp.snapshot} /> : null}
              </section>

              <section className="panel-card">
                <header className="panel-card-head">
                  <h3>Guardrails</h3>
                </header>
                {guardrailsDraft ? (
                  <div className="guardrails-form">
                    <label>
                      <span>Pause</span>
                      <input
                        aria-label="Pause Guardrail"
                        type="checkbox"
                        checked={guardrailsDraft.pause}
                        disabled={liveReadOnly}
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
                        disabled={liveReadOnly}
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
                        disabled={liveReadOnly}
                        onChange={(event) =>
                          setGuardrailsDraft({ ...guardrailsDraft, log_level: event.target.value })
                        }
                      />
                    </label>
                    <button type="button" disabled={liveReadOnly} onClick={() => void handleGuardrailSave()}>
                      保存检查点
                    </button>
                  </div>
                ) : null}
              </section>

              <section className="panel-card">
                <header className="panel-card-head">
                  <h3>阶段 6 评估与导出</h3>
                </header>
                <dl className="metric-grid">
                  <div>
                    <dt>自动指标时间</dt>
                    <dd>{formatTime(automaticMetrics?.computed_at)}</dd>
                  </div>
                  <div>
                    <dt>人工总分</dt>
                    <dd>{manualScore?.overall_score ?? "-"}</dd>
                  </div>
                </dl>
                {automaticMetrics ? <RawJsonCard title="自动指标" value={automaticMetrics} /> : null}
                {manualScore ? (
                  <div className="guardrails-form">
                    {manualScore.scores.length === 0 ? (
                      <label>
                        <span>默认评分项</span>
                        <input
                          aria-label="Manual Score Criterion"
                          value="clarity"
                          onChange={() => undefined}
                          disabled
                        />
                      </label>
                    ) : null}
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
                                        score:
                                          event.target.value === ""
                                            ? null
                                            : Number(event.target.value),
                                      }
                                    : scoreItem,
                                ),
                              })
                            }
                          />
                        </label>
                        <label>
                          <span>Note</span>
                          <input
                            aria-label={`${item.criterion} note`}
                            value={item.note}
                            onChange={(event) =>
                              setManualScore({
                                ...manualScore,
                                scores: manualScore.scores.map((scoreItem, scoreIndex) =>
                                  scoreIndex === index
                                    ? { ...scoreItem, note: event.target.value }
                                    : scoreItem,
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
                    <button type="button" onClick={() => void handleManualScoreSave()}>
                      保存人工评分
                    </button>
                    <button type="button" onClick={() => void handleCreateExport()}>
                      生成分析 ZIP
                    </button>
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

              {historyDetail ? (
                <section className="panel-card">
                  <header className="panel-card-head">
                    <h3>历史档案 / Raw Fallback</h3>
                  </header>
                  <dl className="metric-grid">
                    <div>
                      <dt>Adapter</dt>
                      <dd>{historyDetail.entry.adapter_module_id ?? "unknown"}</dd>
                    </div>
                    <div>
                      <dt>兼容状态</dt>
                      <dd>{historyDetail.entry.adapter_known ? "当前可识别" : "raw fallback"}</dd>
                    </div>
                  </dl>
                  <RawJsonCard title="历史 Raw Manifest" value={historyDetail.raw_manifest} />
                  <RawJsonCard
                    title="历史公共视图"
                    value={{
                      messages: historyDetail.messages,
                      cp_revisions: historyDetail.cp_revisions,
                      logs: historyDetail.logs,
                    }}
                  />
                </section>
              ) : null}

              <RawJsonCard title="Server Logs" value={logs} />
            </div>
          ) : null}

          {selectedInspector !== "server" && profiles ? (
            <AgentInspector
              agent={selectedInspector === "agent-a" ? profiles[0] : profiles[1]}
              runs={runs}
              selectedRunId={selectedRunId}
              setSelectedRunId={setSelectedRunId}
              typingState={currentTyping[selectedInspector === "agent-a" ? profiles[0].agent_id : profiles[1].agent_id]}
              memory={displayedMemory(selectedInspector === "agent-a" ? profiles[0].agent_id : profiles[1].agent_id)}
              contextBundles={contextByAgent[selectedInspector === "agent-a" ? profiles[0].agent_id : profiles[1].agent_id] ?? []}
              attempts={attemptsByAgent[selectedInspector === "agent-a" ? profiles[0].agent_id : profiles[1].agent_id] ?? []}
              logs={logs.filter(
                (item) => item.agent_id === (selectedInspector === "agent-a" ? profiles[0].agent_id : profiles[1].agent_id),
              )}
              onOpenMemoryReview={(revision) =>
                setReviewState({
                  kind: "memory",
                  label: `${selectedInspector} ${revision.revision}`,
                  agentId: selectedInspector === "agent-a" ? profiles[0].agent_id : profiles[1].agent_id,
                  memoryRevision: revision.revision,
                })
              }
            />
          ) : null}
        </section>
      </main>
    </div>
  );
}

function AgentInspector({
  agent,
  runs,
  selectedRunId,
  setSelectedRunId,
  typingState,
  memory,
  contextBundles,
  attempts,
  logs,
  onOpenMemoryReview,
}: {
  agent: AgentProfile;
  runs: AgentRun[];
  selectedRunId: string;
  setSelectedRunId: (runId: string) => void;
  typingState: { active: boolean; runId: string | null; reason?: string } | undefined;
  memory: MemoryRevision[];
  contextBundles: ContextBundle[];
  attempts: AgentRun["attempts"];
  logs: LogEntry[];
  onOpenMemoryReview: (revision: MemoryRevision) => void;
}): JSX.Element {
  const agentRuns = runs.filter((item) => item.agent_id === agent.agent_id);
  const selectedRun = agentRuns.find((item) => item.run_id === selectedRunId) ?? agentRuns[0] ?? null;
  return (
    <div className="inspector-stack" role="tabpanel">
      <section className="panel-card">
        <header className="panel-card-head">
          <h3>{agent.display_name}</h3>
          <span>{typingState?.active ? `typing · ${typingState.reason ?? "running"}` : "idle"}</span>
        </header>
        <p className="agent-meta">{agent.attention_prior}</p>
        <p className="agent-meta">
          {agent.model.provider} / {agent.model.model}
        </p>
      </section>

      <section className="panel-card">
        <header className="panel-card-head">
          <h3>Runs / Attempts</h3>
        </header>
        <div className="timeline">
          {agentRuns.map((run) => (
            <button
              key={run.run_id}
              type="button"
              className={run.run_id === selectedRun?.run_id ? "timeline-item active" : "timeline-item"}
              onClick={() => setSelectedRunId(run.run_id)}
            >
              <strong>{run.status}</strong>
              <span>{run.phase}</span>
              <span>{formatTime(run.started_at)}</span>
            </button>
          ))}
        </div>
        {selectedRun ? <RawJsonCard title="Run Detail" value={selectedRun} /> : null}
        <RawJsonCard title="Attempts" value={attempts} />
      </section>

      <section className="panel-card">
        <header className="panel-card-head">
          <h3>Memory / Context</h3>
        </header>
        <div className="timeline">
          {memory.map((revision) => (
            <button key={revision.revision} type="button" className="timeline-item" onClick={() => onOpenMemoryReview(revision)}>
              <strong>{revision.revision}</strong>
              <span>{formatTime(revision.committed_at)}</span>
            </button>
          ))}
        </div>
        {memory[0]?.module_id === "memory.graph-overlay" ? (
          <RawJsonCard title="Graph Memory" value={memory[0]?.payload ?? {}} />
        ) : (
          <RawJsonCard title="Raw Memory JSON" value={memory[0] ?? {}} />
        )}
        <RawJsonCard title="Context Bundles" value={contextBundles} />
      </section>

      <RawJsonCard title="Agent Logs" value={logs} />
    </div>
  );
}
