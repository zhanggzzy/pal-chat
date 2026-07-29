import "./styles.css";

const state = {
  health: { live: "-", ready: "-", schema: "-" },
  conversations: [],
  currentConversationId: "",
  conversation: null,
  participants: [],
  messages: [],
  topics: [],
  topicDetails: new Map(),
  messageAnalysis: null,
  runTimeline: [],
  selectedMessageId: "",
  selectedTopicId: "",
  mobilePane: "chat",
  autoScroll: true,
  loading: { send: false, create: false, stop: false },
  focusColumnId: "server",
  columnTabs: { server: "config" },
};

const elements = {
  apiBase: document.querySelector("#api-base"),
  healthBadge: document.querySelector("#health-badge"),
  liveStatus: document.querySelector("#live-status"),
  readyStatus: document.querySelector("#ready-status"),
  schemaStatus: document.querySelector("#schema-status"),
  conversationList: document.querySelector("#conversation-list"),
  conversationTitle: document.querySelector("#conversation-title"),
  conversationMeta: document.querySelector("#conversation-meta"),
  deleteConversation: document.querySelector("#delete-conversation"),
  connectionBanner: document.querySelector("#connection-banner"),
  messageList: document.querySelector("#message-list"),
  messageInput: document.querySelector("#message-input"),
  sendMessage: document.querySelector("#send-message"),
  composerHint: document.querySelector("#composer-hint"),
  chatSubtitle: document.querySelector("#chat-subtitle"),
  jumpToLatest: document.querySelector("#jump-to-latest"),
  detailSubtitle: document.querySelector("#detail-subtitle"),
  detailColumns: document.querySelector("#detail-columns"),
  detailColumnPicker: document.querySelector("#detail-column-picker"),
  runDot: document.querySelector("#run-dot"),
  runStatus: document.querySelector("#run-status"),
  messageCount: document.querySelector("#message-count"),
  agentCount: document.querySelector("#agent-count"),
  roundCount: document.querySelector("#round-count"),
  tokenCount: document.querySelector("#token-count"),
  stopRun: document.querySelector("#stop-run"),
  chatPane: document.querySelector("#chat-pane"),
  detailPane: document.querySelector("#detail-pane"),
  mobilePaneTabs: document.querySelectorAll(".segment"),
};

function apiBase() {
  return elements.apiBase.value.replace(/\/$/, "");
}

function requestHeaders(extra = {}) {
  return {
    "Content-Type": "application/json",
    ...extra,
  };
}

async function request(path, init = {}) {
  const response = await fetch(`${apiBase()}${path}`, {
    headers: requestHeaders(init.headers ?? {}),
    ...init,
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const message = data?.error?.message ?? `${response.status} ${response.statusText}`;
    throw new Error(message);
  }
  return data;
}

function setHealthBadge(tone, text) {
  elements.healthBadge.className = `pill ${tone}`;
  elements.healthBadge.textContent = text;
}

function setConnectionBanner(text = "") {
  elements.connectionBanner.textContent = text;
  elements.connectionBanner.classList.toggle("hidden", !text);
}

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function truncate(text, max = 80) {
  if (!text) {
    return "";
  }
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

function humanTime(value) {
  if (!value) {
    return "-";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function longTime(value) {
  if (!value) {
    return "-";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function selectedRun() {
  return state.conversation?.active_run ?? null;
}

function isSmallScreen() {
  return window.matchMedia("(max-width: 1359px)").matches;
}

function isPhoneScreen() {
  return window.matchMedia("(max-width: 479px)").matches;
}

function participantMap() {
  return new Map(state.participants.map((item) => [item.id, item]));
}

function agentParticipants() {
  return state.participants.filter((item) => item.kind === "agent");
}

function topicById(topicId) {
  return state.topics.find((item) => item.id === topicId) ?? null;
}

function topicTitle(topicId) {
  return topicById(topicId)?.title ?? topicId;
}

function activeTopic() {
  const id = state.conversation?.conversation?.active_topic_id;
  return id ? topicById(id) : null;
}

function currentMessage() {
  return state.messages.find((item) => item.id === state.selectedMessageId) ?? null;
}

function analysisForAgent(agentId) {
  const analysis = state.messageAnalysis;
  if (!analysis) {
    return { indexes: [], snapshots: [], snapshotItems: [], decisions: [], calls: [] };
  }

  const snapshots = analysis.snapshots.filter((item) => item.agent_id === agentId);
  const snapshotIds = new Set(snapshots.map((item) => item.id));
  return {
    indexes: analysis.agent_indexes.filter((item) => item.agent_id === agentId),
    snapshots,
    snapshotItems: analysis.snapshot_items.filter((item) => snapshotIds.has(item.snapshot_id)),
    decisions: analysis.decisions.filter((item) => item.agent_id === agentId),
    calls: analysis.model_calls.filter((item) => item.agent_id === agentId),
  };
}

function serverCalls() {
  return (state.messageAnalysis?.model_calls ?? []).filter((item) => !item.agent_id);
}

function serverTopicLinks() {
  return state.messageAnalysis?.topic_links ?? [];
}

function hasPendingAnalysisDecision() {
  return true;
}

function statusTone(run) {
  if (!run) {
    return "idle";
  }
  if (run.status === "queued" || run.status === "running") {
    return "running";
  }
  if (run.status === "stop_requested") {
    return "warning";
  }
  if (run.status === "failed") {
    return "error";
  }
  return "idle";
}

function statusLabel(run) {
  if (!run) {
    return "空闲";
  }
  const labels = {
    queued: "等待中",
    running: "运行中",
    stop_requested: "正在停止",
    stopped: "已停止",
    failed: "失败",
    completed: "已结束",
  };
  return labels[run.status] ?? run.status;
}

function columnDescriptors() {
  return [
    {
      id: "server",
      label: "Server",
      kind: "server",
      avatar: "S",
    },
    ...agentParticipants().map((participant, index) => ({
      id: participant.id,
      label: index === 0 ? "Agent A" : index === 1 ? "Agent B" : participant.display_name,
      kind: "agent",
      avatar: participant.display_name.slice(0, 1),
      participant,
    })),
  ];
}

function ensureColumnState() {
  const descriptors = columnDescriptors();
  const validIds = new Set(descriptors.map((item) => item.id));
  if (!validIds.has(state.focusColumnId)) {
    state.focusColumnId = descriptors[0]?.id ?? "server";
  }
  descriptors.forEach((item) => {
    if (!state.columnTabs[item.id]) {
      state.columnTabs[item.id] = "config";
    }
  });
}

async function loadHealth() {
  setHealthBadge("pending", "检查中");
  try {
    const [live, ready] = await Promise.all([
      request("/health/live"),
      request("/health/ready"),
    ]);
    state.health = {
      live: live.status,
      ready: ready.status,
      schema: ready.schema_revision ?? "-",
    };
    setHealthBadge("ok", "已就绪");
  } catch (error) {
    state.health = { live: "error", ready: "error", schema: "-" };
    setHealthBadge("error", "离线");
    setConnectionBanner(error.message);
  }
  renderHealth();
}

async function loadConversations() {
  try {
    const data = await request("/api/v1/conversations");
    state.conversations = data.items;
    if (!state.currentConversationId && state.conversations[0]) {
      state.currentConversationId = state.conversations[0].id;
    }
    if (
      state.currentConversationId &&
      !state.conversations.some((item) => item.id === state.currentConversationId)
    ) {
      state.currentConversationId = "";
      state.conversation = null;
      state.participants = [];
      state.messages = [];
      state.topics = [];
      state.messageAnalysis = null;
      state.runTimeline = [];
      state.topicDetails.clear();
    }
    renderConversationList();
  } catch (error) {
    setConnectionBanner(error.message);
  }
}

async function loadRunTimeline(runId) {
  if (!runId) {
    state.runTimeline = [];
    return;
  }
  try {
    const data = await request(`/api/v1/runs/${runId}/timeline`);
    state.runTimeline = data.items;
  } catch {
    state.runTimeline = [];
  }
}

async function loadTopicDetail(topicId) {
  if (!topicId || state.topicDetails.has(topicId)) {
    return;
  }
  try {
    const detail = await request(`/api/v1/topics/${topicId}`);
    state.topicDetails.set(topicId, detail);
  } catch (error) {
    setConnectionBanner(error.message);
  }
}

async function loadMessageAnalysis(messageId, { silent = false } = {}) {
  if (!messageId) {
    state.messageAnalysis = null;
    return;
  }
  try {
    state.messageAnalysis = await request(`/api/v1/messages/${messageId}/analysis`);
  } catch (error) {
    state.messageAnalysis = null;
    if (!silent) {
      setConnectionBanner(error.message);
    }
  }
}

async function loadConversation(conversationId) {
  if (!conversationId) {
    renderAll();
    return;
  }
  try {
    const [detail, messages, topics] = await Promise.all([
      request(`/api/v1/conversations/${conversationId}`),
      request(`/api/v1/conversations/${conversationId}/messages`),
      request(`/api/v1/conversations/${conversationId}/topics`),
    ]);
    state.currentConversationId = conversationId;
    state.conversation = detail;
    state.participants = detail.participants;
    state.messages = messages.items;
    state.topics = topics.items;
    ensureColumnState();

    const selected = state.messages.find((item) => item.id === state.selectedMessageId);
    const message = selected ?? state.messages.at(-1) ?? null;
    state.selectedMessageId = message?.id ?? "";
    if (message) {
      await loadMessageAnalysis(message.id, { silent: true });
      const linkedTopicId = state.messageAnalysis?.topic_links?.[0]?.topic_id ?? "";
      state.selectedTopicId = linkedTopicId || state.conversation.conversation.active_topic_id || "";
      if (state.selectedTopicId) {
        await loadTopicDetail(state.selectedTopicId);
      }
    } else {
      state.messageAnalysis = null;
      state.selectedTopicId = state.conversation.conversation.active_topic_id || "";
      if (state.selectedTopicId) {
        await loadTopicDetail(state.selectedTopicId);
      }
    }
    await loadRunTimeline(detail.active_run?.id ?? "");
    renderAll();
    if (state.autoScroll) {
      scrollMessagesToBottom();
    }
  } catch (error) {
    setConnectionBanner(error.message);
  }
}

async function createConversation() {
  state.loading.create = true;
  renderWorkspace();
  try {
    const title = `群聊实验 ${new Date().toLocaleString("zh-CN")}`;
    const data = await request("/api/v1/conversations", {
      method: "POST",
      body: JSON.stringify({ title }),
    });
    await loadConversations();
    await loadConversation(data.conversation.id);
  } catch (error) {
    setConnectionBanner(error.message);
  } finally {
    state.loading.create = false;
    renderWorkspace();
  }
}

async function deleteConversation() {
  if (!state.currentConversationId) {
    return;
  }
  if (!window.confirm("确定删除此会话？此操作不可撤销。")) {
    return;
  }
  try {
    await request(`/api/v1/conversations/${state.currentConversationId}`, {
      method: "DELETE",
    });
    state.currentConversationId = "";
    state.conversation = null;
    state.participants = [];
    state.messages = [];
    state.topics = [];
    state.messageAnalysis = null;
    state.runTimeline = [];
    state.topicDetails.clear();
    await loadConversations();
    if (state.currentConversationId) {
      await loadConversation(state.currentConversationId);
    } else {
      renderAll();
    }
  } catch (error) {
    setConnectionBanner(error.message);
  }
}

async function sendMessage(event) {
  event.preventDefault();
  if (!state.currentConversationId || state.loading.send) {
    return;
  }
  const content = elements.messageInput.value.trim();
  if (!content) {
    return;
  }
  state.loading.send = true;
  renderComposer();
  try {
    await request(`/api/v1/conversations/${state.currentConversationId}/messages`, {
      method: "POST",
      headers: { "Idempotency-Key": `web-${Date.now()}` },
      body: JSON.stringify({ content }),
    });
    elements.messageInput.value = "";
    autoResizeTextarea();
    await loadConversations();
    await loadConversation(state.currentConversationId);
  } catch (error) {
    setConnectionBanner(error.message);
  } finally {
    state.loading.send = false;
    renderComposer();
  }
}

async function stopRun() {
  const run = selectedRun();
  if (!run || state.loading.stop) {
    return;
  }
  state.loading.stop = true;
  renderStatusBar();
  try {
    await request(`/api/v1/runs/${run.id}/stop`, {
      method: "POST",
      headers: { "Idempotency-Key": `stop-${Date.now()}` },
    });
    await loadConversations();
    await loadConversation(state.currentConversationId);
  } catch (error) {
    setConnectionBanner(error.message);
  } finally {
    state.loading.stop = false;
    renderStatusBar();
  }
}

function selectConversation(conversationId) {
  if (!conversationId || conversationId === state.currentConversationId) {
    return;
  }
  state.topicDetails.clear();
  state.selectedTopicId = "";
  loadConversation(conversationId);
}

async function selectMessage(messageId) {
  if (!messageId) {
    return;
  }
  state.selectedMessageId = messageId;
  const message = currentMessage();
  if (!message) {
    return;
  }
  await loadMessageAnalysis(message.id);
  const firstLink = state.messageAnalysis?.topic_links?.[0]?.topic_id ?? "";
  if (firstLink) {
    state.selectedTopicId = firstLink;
    await loadTopicDetail(firstLink);
  }
  const author = participantMap().get(message.author_participant_id);
  if (author?.kind === "agent") {
    state.focusColumnId = author.id;
    state.columnTabs[author.id] = "log";
  } else {
    state.focusColumnId = "server";
    state.columnTabs.server = "context";
  }
  renderAll();
}

function setColumnTab(columnId, tab) {
  state.columnTabs[columnId] = tab;
  renderDetailPanel();
}

function setMobilePane(pane) {
  state.mobilePane = pane;
  renderWorkspace();
}

function setFocusColumn(columnId) {
  state.focusColumnId = columnId;
  renderDetailPanel();
}

function autoResizeTextarea() {
  elements.messageInput.style.height = "auto";
  elements.messageInput.style.height = `${Math.min(elements.messageInput.scrollHeight, 164)}px`;
}

function scrollMessagesToBottom() {
  elements.messageList.scrollTop = elements.messageList.scrollHeight;
  state.autoScroll = true;
  renderJumpButton();
}

function renderHealth() {
  elements.liveStatus.textContent = state.health.live;
  elements.readyStatus.textContent = state.health.ready;
  elements.schemaStatus.textContent = state.health.schema;
}

function renderConversationList() {
  if (!state.conversations.length) {
    elements.conversationList.innerHTML = `
      <div class="empty-card">
        <strong>尚无会话</strong>
        <p>点击“新会话”开始群聊实验。</p>
      </div>
    `;
    return;
  }

  elements.conversationList.innerHTML = state.conversations
    .map(
      (item) => `
        <button
          type="button"
          class="session-card ${item.id === state.currentConversationId ? "active" : ""}"
          data-conversation-id="${item.id}"
        >
          <div class="session-card-top">
            <strong>${escapeHtml(item.title)}</strong>
            <span class="mini-pill ${item.active_run_status ? "pending" : "idle"}">
              ${item.active_run_status ?? "空闲"}
            </span>
          </div>
          <p>${item.last_message_preview ? escapeHtml(truncate(item.last_message_preview, 72)) : "尚无消息"}</p>
          <div class="session-card-meta">
            <span>${item.message_count} 条消息</span>
            <span>${longTime(item.updated_at)}</span>
          </div>
        </button>
      `
    )
    .join("");
}

function renderWorkspace() {
  const detail = state.conversation;
  if (!detail) {
    elements.conversationTitle.textContent = state.loading.create ? "正在创建会话..." : "未选择会话";
    elements.conversationMeta.textContent = "创建新会话后开始群聊实验。";
    elements.deleteConversation.disabled = true;
  } else {
    const run = detail.active_run;
    elements.conversationTitle.textContent = detail.conversation.title;
    elements.conversationMeta.textContent = run
      ? `会话进行中，状态：${statusLabel(run)}`
      : `创建于 ${longTime(detail.conversation.created_at)}`;
    elements.deleteConversation.disabled = false;
  }

  const showChat = !isSmallScreen() || state.mobilePane === "chat";
  const showDetail = !isSmallScreen() || state.mobilePane === "detail";
  elements.chatPane.classList.toggle("hidden-pane", !showChat);
  elements.detailPane.classList.toggle("hidden-pane", !showDetail);
  elements.mobilePaneTabs.forEach((button) => {
    button.classList.toggle("active", button.dataset.pane === state.mobilePane);
  });
}

function renderJumpButton() {
  elements.jumpToLatest.classList.toggle("hidden", state.autoScroll);
}

function renderMessages() {
  if (!state.messages.length) {
    elements.messageList.innerHTML = `
      <div class="empty-state">
        <h4>开始群聊实验</h4>
        <p>发送一条消息，观察两个 Agent 如何参与对话。</p>
      </div>
    `;
    return;
  }

  const people = participantMap();
  elements.messageList.innerHTML = state.messages
    .map((message, index) => {
      const person = people.get(message.author_participant_id);
      const previous = state.messages[index - 1];
      const compact = previous && previous.author_participant_id === message.author_participant_id;
      const name = person?.kind === "user" ? "我" : person?.display_name ?? "Agent";
      return `
        <article
          class="message-row ${message.kind === "user" ? "user" : "agent"} ${message.id === state.selectedMessageId ? "selected" : ""}"
          data-message-id="${message.id}"
          tabindex="0"
          aria-label="${name} 在 ${humanTime(message.created_at)} 发送的消息"
        >
          <div class="message-meta ${compact ? "compact" : ""}">
            <span class="avatar">${escapeHtml(name.slice(0, 1))}</span>
            <div>
              <strong>${escapeHtml(name)}</strong>
              <span>${humanTime(message.created_at)}</span>
            </div>
          </div>
          <div class="message-bubble">
            <p>${escapeHtml(message.content).replace(/\n/g, "<br />")}</p>
          </div>
        </article>
      `;
    })
    .join("");

  if (state.autoScroll) {
    requestAnimationFrame(scrollMessagesToBottom);
  }
}

function renderComposer() {
  const hasConversation = Boolean(state.currentConversationId);
  const run = selectedRun();
  const disabled = !hasConversation || Boolean(run);
  elements.messageInput.disabled = disabled;
  elements.sendMessage.disabled = disabled || state.loading.send || !elements.messageInput.value.trim();
  elements.composerHint.textContent = !hasConversation
    ? "请先创建或选择会话。"
    : run
      ? run.status === "stop_requested"
        ? "正在停止..."
        : "等待自动接力结束..."
      : state.loading.send
        ? "发送中..."
        : "空消息不可发送。";
  elements.chatSubtitle.textContent = hasConversation
    ? `当前会话包含 ${state.messages.length} 条消息。`
    : "发送一条消息，观察两个 Agent 如何参与对话。";
}

function renderStatusBar() {
  const run = selectedRun();
  elements.runDot.className = `dot ${statusTone(run)}`;
  elements.runStatus.textContent = statusLabel(run);
  elements.messageCount.textContent = String(state.messages.length);
  elements.agentCount.textContent = String(agentParticipants().length);
  elements.roundCount.textContent = run ? `${run.round_count} / ${run.max_rounds}` : "0 / 0";
  elements.tokenCount.textContent = run ? `${run.actual_tokens} / ${run.max_total_tokens}` : "0 / 0";
  elements.stopRun.classList.toggle("hidden", !run);
  elements.stopRun.disabled = !run || state.loading.stop;
  elements.stopRun.textContent = state.loading.stop ? "停止中..." : "停止";
}

function renderServerConfig() {
  const conversation = state.conversation?.conversation;
  const run = selectedRun();
  const participants = state.participants
    .map(
      (item) => `
        <div class="mini-card">
          <div class="mini-card-top">
            <strong>${escapeHtml(item.display_name)}</strong>
            <span class="mini-pill ${item.kind === "agent" ? "ok" : "idle"}">${item.kind}</span>
          </div>
          <p>${escapeHtml(item.role_prompt ?? "无角色说明")}</p>
        </div>
      `
    )
    .join("");

  return `
    <div class="column-stack">
      <div class="info-grid">
        <div><span>conversation_id</span><strong>${conversation?.id ?? "-"}</strong></div>
        <div><span>active_topic</span><strong>${conversation?.active_topic_id ?? "-"}</strong></div>
        <div><span>created_at</span><strong>${longTime(conversation?.created_at)}</strong></div>
      </div>
      <div class="detail-section">
        <h5>参与者</h5>
        ${participants || "<p class='muted'>暂无配置信息。</p>"}
      </div>
      <div class="detail-section">
        <h5>运行限制</h5>
        <div class="info-grid">
          <div><span>max_messages</span><strong>${run?.max_agent_messages ?? 5}</strong></div>
          <div><span>max_rounds</span><strong>${run?.max_rounds ?? 6}</strong></div>
          <div><span>max_tokens</span><strong>${run?.max_total_tokens ?? 30000}</strong></div>
          <div><span>timeout_ms</span><strong>${run?.timeout_ms ?? 90000}</strong></div>
        </div>
      </div>
    </div>
  `;
}

function renderServerContext() {
  const active = activeTopic();
  const detail = state.selectedTopicId ? state.topicDetails.get(state.selectedTopicId) : null;
  const links = serverTopicLinks();

  return `
    <div class="column-stack">
      <div class="pending-note">
        <strong>待定</strong>
        <p>最终话题 / 快照 / 调用入口仍待用户确认，这里先按 v2 草案映射展示系统级上下文。</p>
      </div>
      <div class="detail-section">
        <h5>当前 active 话题</h5>
        ${
          active
            ? `
              <article class="topic-summary-card">
                <div class="mini-card-top">
                  <strong>${escapeHtml(active.title)}</strong>
                  <span class="mini-pill ${active.status === "active" ? "ok" : "idle"}">${active.status}</span>
                </div>
                <p>${active.summary_count ? `${active.summary_count} 个摘要` : "尚无摘要"}</p>
              </article>
            `
            : "<p class='muted'>尚无话题，发送一条消息开始对话。</p>"
        }
      </div>
      <div class="detail-section">
        <h5>消息话题关联</h5>
        ${
          links.length
            ? links
                .map(
                  (item) => `
                    <article class="mini-card">
                      <div class="mini-card-top">
                        <strong>${escapeHtml(topicTitle(item.topic_id))}</strong>
                        <span>${escapeHtml(item.route_action)}</span>
                      </div>
                      <p>kind: ${escapeHtml(item.kind)} / confidence: ${item.confidence.toFixed(2)}</p>
                    </article>
                  `
                )
                .join("")
            : "<p class='muted'>尚无上下文数据。</p>"
        }
      </div>
      <div class="detail-section">
        <h5>话题转换链</h5>
        ${
          detail?.transitions?.length
            ? detail.transitions
                .map(
                  (item) => `
                    <div class="timeline-row">
                      <strong>${escapeHtml(item.action)}</strong>
                      <span>${longTime(item.created_at)}</span>
                    </div>
                  `
                )
                .join("")
            : "<p class='muted'>尚无话题转换记录。</p>"
        }
      </div>
    </div>
  `;
}

function renderServerLog() {
  return `
    <div class="column-stack">
      <div class="pending-note">
        <strong>待定</strong>
        <p>系统级 log 会保留，但最终是否与独立功能入口并存仍待用户确认。</p>
      </div>
      <div class="detail-section">
        <h5>运行事件</h5>
        ${
          state.runTimeline.length
            ? state.runTimeline
                .map(
                  (item) => `
                    <div class="timeline-row">
                      <strong>${escapeHtml(item.type)}</strong>
                      <span>#${item.seq} · ${humanTime(item.occurred_at)}</span>
                    </div>
                  `
                )
                .join("")
            : "<p class='muted'>尚无事件或决策记录。</p>"
        }
      </div>
      <div class="detail-section">
        <h5>模型调用总览</h5>
        ${
          serverCalls().length
            ? serverCalls()
                .map(
                  (call) => `
                    <article class="mini-card">
                      <div class="mini-card-top">
                        <strong>${escapeHtml(call.purpose)}</strong>
                        <span>${escapeHtml(call.status)}</span>
                      </div>
                      <p>${call.input_tokens} / ${call.output_tokens} tokens，${call.latency_ms} ms</p>
                    </article>
                  `
                )
                .join("")
            : "<p class='muted'>暂无系统级数据。</p>"
        }
      </div>
    </div>
  `;
}

function renderAgentConfig(participant) {
  const data = analysisForAgent(participant.id);
  return `
    <div class="column-stack">
      <div class="mini-card">
        <div class="mini-card-top">
          <strong>${escapeHtml(participant.display_name)}</strong>
          <span class="mini-pill ok">agent</span>
        </div>
        <p>${escapeHtml(participant.role_prompt ?? "无角色说明")}</p>
      </div>
      <div class="info-grid">
        <div><span>sort_order</span><strong>${participant.sort_order}</strong></div>
        <div><span>threshold</span><strong>${participant.decision_threshold ?? "-"}</strong></div>
      </div>
      <div class="detail-section">
        <h5>话题索引摘要</h5>
        ${
          data.indexes.length
            ? data.indexes
                .map(
                  (item) => `
                    <article class="mini-card">
                      <div class="mini-card-top">
                        <strong>${escapeHtml(topicTitle(item.topic_id))}</strong>
                        <span>${longTime(item.updated_at)}</span>
                      </div>
                      <p>salience ${item.salience.toFixed(2)} / role ${item.role_affinity.toFixed(2)} / familiarity ${item.familiarity.toFixed(2)}</p>
                    </article>
                  `
                )
                .join("")
            : "<p class='muted'>暂无配置信息。</p>"
        }
      </div>
    </div>
  `;
}

function renderAgentContext(participant) {
  const data = analysisForAgent(participant.id);
  const snapshotItemsById = new Map();
  data.snapshotItems.forEach((item) => {
    const list = snapshotItemsById.get(item.snapshot_id) ?? [];
    list.push(item);
    snapshotItemsById.set(item.snapshot_id, list);
  });

  return `
    <div class="column-stack">
      <div class="pending-note">
        <strong>待定</strong>
        <p>上下文面板已按 Agent 列组织，但最终是否还保留独立快照入口，仍待用户确认。</p>
      </div>
      ${
        data.snapshots.length
          ? data.snapshots
              .map(
                (snapshot) => `
                  <section class="detail-card">
                    <div class="mini-card-top">
                      <strong>${escapeHtml(snapshot.purpose)}</strong>
                      <span>${snapshot.estimated_tokens} tokens</span>
                    </div>
                    <p class="muted">tokenizer: ${escapeHtml(snapshot.tokenizer)}</p>
                    ${
                      (snapshotItemsById.get(snapshot.id) ?? [])
                        .map(
                          (item) => `
                            <article class="snapshot-item">
                              <div class="mini-card-top">
                                <strong>${escapeHtml(item.item_type)}</strong>
                                <span>${item.estimated_tokens} tokens</span>
                              </div>
                              <p>${escapeHtml(truncate(item.rendered_content, 120))}</p>
                              <p class="muted">原因：${escapeHtml(item.inclusion_reason)}</p>
                            </article>
                          `
                        )
                        .join("") || "<p class='muted'>暂无快照条目。</p>"
                    }
                  </section>
                `
              )
              .join("")
          : "<p class='muted'>尚无上下文数据。</p>"
      }
    </div>
  `;
}

function decisionOutcomeLabel(outcome) {
  const labels = {
    selected: "选中发言",
    eligible_not_selected: "想回应但未选中",
    silent: "沉默",
    ineligible: "不具资格",
  };
  return labels[outcome] ?? outcome;
}

function renderAgentLog(participant) {
  const data = analysisForAgent(participant.id);
  return `
    <div class="column-stack">
      <div class="pending-note">
        <strong>待定</strong>
        <p>当前先按 v2 草案把决策与调用日志归在 Agent 列内，最终入口待用户确认。</p>
      </div>
      <div class="detail-section">
        <h5>决策历史</h5>
        ${
          data.decisions.length
            ? data.decisions
                .map(
                  (item) => `
                    <article class="decision-card">
                      <div class="mini-card-top">
                        <strong>${escapeHtml(decisionOutcomeLabel(item.outcome))}</strong>
                        <span>${item.score.toFixed(2)} / ${item.threshold.toFixed(2)}</span>
                      </div>
                      <p>intent: ${escapeHtml(item.reply_intent)}</p>
                      <p class="muted">${escapeHtml((item.reason_codes_json ?? []).join(" / ") || "无原因码")}</p>
                    </article>
                  `
                )
                .join("")
            : "<p class='muted'>尚无事件或决策记录。</p>"
        }
      </div>
      <div class="detail-section">
        <h5>模型调用</h5>
        ${
          data.calls.length
            ? data.calls
                .map(
                  (call) => `
                    <article class="mini-card">
                      <div class="mini-card-top">
                        <strong>${escapeHtml(call.purpose)}</strong>
                        <span>${escapeHtml(call.status)}</span>
                      </div>
                      <p>${call.input_tokens} / ${call.output_tokens} tokens，${call.latency_ms} ms</p>
                    </article>
                  `
                )
                .join("")
            : "<p class='muted'>尚无模型调用记录。</p>"
        }
      </div>
    </div>
  `;
}

function renderColumnContent(descriptor, tab) {
  if (descriptor.kind === "server") {
    if (tab === "config") {
      return renderServerConfig();
    }
    if (tab === "context") {
      return renderServerContext();
    }
    return renderServerLog();
  }
  if (tab === "config") {
    return renderAgentConfig(descriptor.participant);
  }
  if (tab === "context") {
    return renderAgentContext(descriptor.participant);
  }
  return renderAgentLog(descriptor.participant);
}

function renderColumnPicker() {
  const descriptors = columnDescriptors();
  elements.detailColumnPicker.innerHTML = descriptors
    .map(
      (item) => `
        <button
          type="button"
          class="column-chip ${item.id === state.focusColumnId ? "active" : ""}"
          data-column-focus="${item.id}"
        >
          ${escapeHtml(item.label)}
        </button>
      `
    )
    .join("");
}

function renderDetailPanel() {
  ensureColumnState();
  renderColumnPicker();
  const descriptors = columnDescriptors().filter((item) => !isSmallScreen() || item.id === state.focusColumnId);
  elements.detailSubtitle.textContent = hasPendingAnalysisDecision()
    ? "三列结构已对齐 spec-v2；最终分析入口待用户确认。"
    : "三列结构已定稿。";

  elements.detailColumns.innerHTML = descriptors
    .map((descriptor) => {
      const activeTab = state.columnTabs[descriptor.id] ?? "config";
      return `
        <section class="detail-column ${descriptor.kind === "server" ? "server-column" : ""}">
          <div class="column-header">
            <div class="column-title">
              <span class="avatar ${descriptor.kind === "server" ? "server-avatar" : ""}">${escapeHtml(descriptor.avatar)}</span>
              <div>
                <strong>${escapeHtml(descriptor.label)}</strong>
                <span>${descriptor.kind === "server" ? "系统级信息" : escapeHtml(descriptor.participant.display_name)}</span>
              </div>
            </div>
            <span class="pulse-dot ${descriptor.id === state.focusColumnId ? "active" : ""}"></span>
          </div>
          <div class="column-tabs" role="tablist" aria-label="${escapeHtml(descriptor.label)} tabs">
            ${["config", "context", "log"]
              .map(
                (tab) => `
                  <button
                    type="button"
                    class="column-tab ${tab === activeTab ? "active" : ""}"
                    data-column-id="${descriptor.id}"
                    data-column-tab="${tab}"
                  >
                    ${tab}
                  </button>
                `
              )
              .join("")}
          </div>
          <div class="column-body">
            ${renderColumnContent(descriptor, activeTab)}
          </div>
          <button type="button" class="reset-button" disabled>Reset</button>
        </section>
      `;
    })
    .join("");
}

function renderAll() {
  renderHealth();
  renderConversationList();
  renderWorkspace();
  renderMessages();
  renderComposer();
  renderDetailPanel();
  renderStatusBar();
}

elements.apiBase.addEventListener("change", async () => {
  setConnectionBanner("");
  state.topicDetails.clear();
  await loadHealth();
  await loadConversations();
  if (state.currentConversationId) {
    await loadConversation(state.currentConversationId);
  }
});

document.querySelector("#refresh-health").addEventListener("click", loadHealth);
document.querySelector("#refresh-conversations").addEventListener("click", async () => {
  await loadConversations();
  if (state.currentConversationId) {
    await loadConversation(state.currentConversationId);
  }
});
document.querySelector("#create-conversation").addEventListener("click", createConversation);
document.querySelector("#delete-conversation").addEventListener("click", deleteConversation);
document.querySelector("#composer").addEventListener("submit", sendMessage);
elements.stopRun.addEventListener("click", stopRun);
elements.jumpToLatest.addEventListener("click", scrollMessagesToBottom);

elements.messageInput.addEventListener("input", () => {
  autoResizeTextarea();
  renderComposer();
});

elements.messageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    document.querySelector("#composer").requestSubmit();
  }
});

elements.messageList.addEventListener("scroll", () => {
  const delta =
    elements.messageList.scrollHeight -
    elements.messageList.scrollTop -
    elements.messageList.clientHeight;
  state.autoScroll = delta < 24;
  renderJumpButton();
});

document.addEventListener("click", (event) => {
  const conversationButton = event.target.closest("[data-conversation-id]");
  if (conversationButton) {
    selectConversation(conversationButton.dataset.conversationId);
    return;
  }

  const messageButton = event.target.closest("[data-message-id]");
  if (messageButton) {
    selectMessage(messageButton.dataset.messageId);
    return;
  }

  const columnTabButton = event.target.closest("[data-column-tab]");
  if (columnTabButton) {
    setColumnTab(columnTabButton.dataset.columnId, columnTabButton.dataset.columnTab);
    return;
  }

  const columnFocusButton = event.target.closest("[data-column-focus]");
  if (columnFocusButton) {
    setFocusColumn(columnFocusButton.dataset.columnFocus);
    return;
  }
});

elements.mobilePaneTabs.forEach((button) => {
  button.addEventListener("click", () => setMobilePane(button.dataset.pane));
});

window.addEventListener("resize", renderAll);

async function boot() {
  await loadHealth();
  await loadConversations();
  if (state.currentConversationId) {
    await loadConversation(state.currentConversationId);
  } else {
    renderAll();
  }
  autoResizeTextarea();
}

boot();
