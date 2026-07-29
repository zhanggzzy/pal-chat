import "./styles.css";

const state = {
  apiBase: "http://127.0.0.1:8000",
  health: { live: "-", ready: "-", schema: "-" },
  conversations: [],
  currentConversationId: "",
  conversation: null,
  participants: [],
  messages: [],
  topics: [],
  selectedMessageId: "",
  selectedTopicId: "",
  topicDetails: new Map(),
  messageAnalysis: null,
  activeTab: "topics",
  mobilePane: "chat",
  autoScroll: true,
  loading: { send: false, create: false, stop: false },
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
  messageList: document.querySelector("#message-list"),
  messageInput: document.querySelector("#message-input"),
  composerHint: document.querySelector("#composer-hint"),
  sendMessage: document.querySelector("#send-message"),
  jumpToLatest: document.querySelector("#jump-to-latest"),
  chatSubtitle: document.querySelector("#chat-subtitle"),
  analysisSubtitle: document.querySelector("#analysis-subtitle"),
  analysisContent: document.querySelector("#analysis-content"),
  connectionBanner: document.querySelector("#connection-banner"),
  runDot: document.querySelector("#run-dot"),
  runStatus: document.querySelector("#run-status"),
  messageCount: document.querySelector("#message-count"),
  agentCount: document.querySelector("#agent-count"),
  roundCount: document.querySelector("#round-count"),
  tokenCount: document.querySelector("#token-count"),
  stopRun: document.querySelector("#stop-run"),
  chatPane: document.querySelector("#chat-pane"),
  analysisPane: document.querySelector("#analysis-pane"),
  mobileTabs: document.querySelectorAll(".segment"),
  analysisTabs: document.querySelectorAll(".tab"),
};

function getApiBase() {
  return elements.apiBase.value.replace(/\/$/, "");
}

function setHealthBadge(tone, text) {
  elements.healthBadge.className = `pill ${tone}`;
  elements.healthBadge.textContent = text;
}

function setConnectionBanner(text = "") {
  elements.connectionBanner.textContent = text;
  elements.connectionBanner.classList.toggle("hidden", !text);
}

function participantMap() {
  return new Map(state.participants.map((item) => [item.id, item]));
}

function selectedRun() {
  return state.conversation?.active_run ?? null;
}

function isConversationBusy() {
  return Boolean(selectedRun());
}

function isSmallScreen() {
  return window.matchMedia("(max-width: 899px)").matches;
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

function truncate(text, max = 120) {
  if (!text) {
    return "";
  }
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

async function request(path, init = {}) {
  const response = await fetch(`${getApiBase()}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
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
    }
    renderConversationList();
  } catch (error) {
    setConnectionBanner(error.message);
  }
}

async function loadConversation(conversationId) {
  if (!conversationId) {
    renderWorkspace();
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
    if (!state.selectedTopicId && state.topics[0]) {
      state.selectedTopicId = state.topics[0].id;
    }
    const latestMessage = state.messages.at(-1);
    if (latestMessage) {
      state.selectedMessageId = latestMessage.id;
      await loadMessageAnalysis(latestMessage.id, { silent: true });
    } else {
      state.selectedMessageId = "";
      state.messageAnalysis = null;
    }
    renderAll();
    scrollMessagesToBottom();
  } catch (error) {
    setConnectionBanner(error.message);
  }
}

async function loadMessageAnalysis(messageId, { silent = false } = {}) {
  if (!messageId) {
    state.messageAnalysis = null;
    renderAnalysis();
    return;
  }
  try {
    state.messageAnalysis = await request(`/api/v1/messages/${messageId}/analysis`);
    renderAnalysis();
  } catch (error) {
    state.messageAnalysis = null;
    if (!silent) {
      setConnectionBanner(error.message);
    }
    renderAnalysis();
  }
}

async function loadTopicDetail(topicId) {
  if (!topicId || state.topicDetails.has(topicId)) {
    return;
  }
  try {
    const detail = await request(`/api/v1/topics/${topicId}`);
    state.topicDetails.set(topicId, detail);
    renderAnalysis();
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
  const confirmed = window.confirm("确定删除此会话？此操作不可撤销。");
  if (!confirmed) {
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

function selectMessage(messageId) {
  state.selectedMessageId = messageId;
  renderMessages();
  loadMessageAnalysis(messageId);
}

function selectTopic(topicId) {
  state.selectedTopicId = topicId;
  renderAnalysis();
  loadTopicDetail(topicId);
}

function setActiveTab(tab) {
  state.activeTab = tab;
  renderAnalysis();
}

function setMobilePane(pane) {
  state.mobilePane = pane;
  renderWorkspace();
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
            <strong>${item.title}</strong>
            <span class="mini-pill ${item.active_run_status ? "pending" : "idle"}">
              ${item.active_run_status ?? "空闲"}
            </span>
          </div>
          <p>${item.last_message_preview ? truncate(item.last_message_preview, 72) : "尚无消息"}</p>
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
    const activeRun = detail.active_run;
    elements.conversationTitle.textContent = detail.conversation.title;
    elements.conversationMeta.textContent = activeRun
      ? `会话进行中，状态：${statusLabel(activeRun)}`
      : `创建于 ${longTime(detail.conversation.created_at)}`;
    elements.deleteConversation.disabled = false;
  }

  const showChat = !isSmallScreen() || state.mobilePane === "chat";
  const showAnalysis = !isSmallScreen() || state.mobilePane === "analysis";
  elements.chatPane.classList.toggle("hidden-pane", !showChat);
  elements.analysisPane.classList.toggle("hidden-pane", !showAnalysis);
  elements.mobileTabs.forEach((button) => {
    button.classList.toggle("active", button.dataset.pane === state.mobilePane);
  });
}

function renderJumpButton() {
  elements.jumpToLatest.classList.toggle("hidden", state.autoScroll);
}

function renderMessages() {
  const people = participantMap();
  if (!state.messages.length) {
    elements.messageList.innerHTML = `
      <div class="empty-state">
        <h4>开始群聊实验</h4>
        <p>发送一条消息，观察两个 Agent 如何参与对话。</p>
      </div>
    `;
    return;
  }

  elements.messageList.innerHTML = state.messages
    .map((message, index) => {
      const person = people.get(message.author_participant_id);
      const previous = state.messages[index - 1];
      const collapseMeta =
        previous && previous.author_participant_id === message.author_participant_id;
      const name = person?.kind === "user" ? "我" : person?.display_name ?? "Agent";
      return `
        <article
          class="message-row ${message.kind === "user" ? "user" : "agent"} ${message.id === state.selectedMessageId ? "selected" : ""}"
          data-message-id="${message.id}"
          tabindex="0"
          aria-label="${name} 在 ${humanTime(message.created_at)} 发送的消息"
        >
          <div class="message-meta ${collapseMeta ? "compact" : ""}">
            <span class="avatar">${name.slice(0, 1)}</span>
            <div>
              <strong>${name}</strong>
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
  const inputDisabled = !hasConversation || Boolean(run);
  elements.messageInput.disabled = inputDisabled;
  elements.sendMessage.disabled =
    inputDisabled || state.loading.send || !elements.messageInput.value.trim();
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
  elements.agentCount.textContent = String(
    state.participants.filter((item) => item.kind === "agent").length
  );
  elements.roundCount.textContent = run
    ? `${run.round_count} / ${run.max_rounds}`
    : "0 / 0";
  elements.tokenCount.textContent = run
    ? `${run.actual_tokens} / ${run.max_total_tokens}`
    : "0 / 0";
  elements.stopRun.classList.toggle("hidden", !run);
  elements.stopRun.disabled = !run || state.loading.stop;
  elements.stopRun.textContent = state.loading.stop ? "停止中..." : "停止";
}

function renderAnalysis() {
  elements.analysisTabs.forEach((button) => {
    const active = button.dataset.tab === state.activeTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });

  const hasSelection = Boolean(state.selectedMessageId);
  elements.analysisSubtitle.textContent = hasSelection
    ? `已选消息：#${state.messages.find((item) => item.id === state.selectedMessageId)?.sequence_no ?? "-"}`
    : "选中一条消息后查看话题、Agent、快照和调用明细。";

  if (!hasSelection) {
    elements.analysisContent.innerHTML = `
      <div class="empty-state">
        <h4>尚无分析对象</h4>
        <p>先在聊天区选择一条消息。</p>
      </div>
    `;
    return;
  }

  if (state.activeTab === "topics") {
    renderTopicsTab();
    return;
  }
  if (state.activeTab === "agents") {
    renderAgentsTab();
    return;
  }
  if (state.activeTab === "snapshots") {
    renderSnapshotsTab();
    return;
  }
  renderCallsTab();
}

function renderTopicsTab() {
  const detail = state.selectedTopicId ? state.topicDetails.get(state.selectedTopicId) : null;
  const listHtml = state.topics.length
    ? state.topics
        .map(
          (topic) => `
            <button
              type="button"
              class="topic-card ${topic.id === state.selectedTopicId ? "active" : ""}"
              data-topic-id="${topic.id}"
            >
              <div class="topic-card-top">
                <strong>${escapeHtml(topic.title)}</strong>
                <span class="mini-pill ${topic.status === "active" ? "ok" : "idle"}">${topic.status}</span>
              </div>
              <div class="topic-card-meta">
                <span>${topic.message_count} 条消息</span>
                <span>${topic.transition_count} 次切换</span>
                <span>${topic.summary_count} 个摘要</span>
              </div>
            </button>
          `
        )
        .join("")
    : `
      <div class="empty-card">
        <strong>尚无话题</strong>
        <p>发送一条消息开始对话。</p>
      </div>
    `;

  const detailHtml = detail
    ? `
      <div class="detail-card">
        <h4>${escapeHtml(detail.topic.title)}</h4>
        <p class="muted">状态：${detail.topic.status}，最后更新 ${longTime(detail.topic.updated_at)}</p>
        <div class="detail-section">
          <h5>状态转换</h5>
          ${
            detail.transitions.length
              ? detail.transitions
                  .map(
                    (item) => `
                      <div class="timeline-row">
                        <strong>${item.action}</strong>
                        <span>${longTime(item.created_at)}</span>
                      </div>
                    `
                  )
                  .join("")
              : "<p class='muted'>暂无转换记录。</p>"
          }
        </div>
        <div class="detail-section">
          <h5>摘要修订</h5>
          ${
            detail.summaries.length
              ? detail.summaries
                  .map(
                    (item) => `
                      <article class="summary-card">
                        <div class="timeline-row">
                          <strong>through ${item.through_message_id}</strong>
                          <span>${item.estimated_tokens} tokens</span>
                        </div>
                        <p>${escapeHtml(item.content)}</p>
                      </article>
                    `
                  )
                  .join("")
              : "<p class='muted'>尚无摘要。</p>"
          }
        </div>
      </div>
    `
    : `
      <div class="detail-card empty-card">
        <strong>选择一个话题</strong>
        <p>查看状态转换与摘要修订。</p>
      </div>
    `;

  elements.analysisContent.innerHTML = `
    <div class="analysis-grid">
      <div class="analysis-column">${listHtml}</div>
      <div class="analysis-column">${detailHtml}</div>
    </div>
  `;
}

function renderAgentsTab() {
  const analysis = state.messageAnalysis;
  const agents = state.participants.filter((item) => item.kind === "agent");
  if (!analysis || !agents.length) {
    elements.analysisContent.innerHTML = `
      <div class="empty-state">
        <h4>尚无决策数据</h4>
        <p>当前消息还没有关联的 Agent 决策。</p>
      </div>
    `;
    return;
  }

  const indexesByAgent = new Map();
  analysis.agent_indexes.forEach((item) => {
    const list = indexesByAgent.get(item.agent_id) ?? [];
    list.push(item);
    indexesByAgent.set(item.agent_id, list);
  });
  const decisionsByAgent = new Map();
  analysis.decisions.forEach((item) => {
    const list = decisionsByAgent.get(item.agent_id) ?? [];
    list.push(item);
    decisionsByAgent.set(item.agent_id, list);
  });

  elements.analysisContent.innerHTML = agents
    .map((agent) => {
      const indexes = indexesByAgent.get(agent.id) ?? [];
      const decisions = decisionsByAgent.get(agent.id) ?? [];
      return `
        <section class="agent-panel">
          <div class="agent-heading">
            <div class="avatar large">${agent.display_name.slice(0, 1)}</div>
            <div>
              <h4>${agent.display_name}</h4>
              <p class="muted">${escapeHtml(agent.role_prompt ?? "无角色说明")}</p>
            </div>
          </div>

          <div class="detail-section">
            <h5>话题索引</h5>
            ${
              indexes.length
                ? indexes
                    .map(
                      (item) => `
                        <article class="metric-card">
                          <div class="timeline-row">
                            <strong>${topicTitle(item.topic_id)}</strong>
                            <span>${longTime(item.updated_at)}</span>
                          </div>
                          <p>salience ${item.salience.toFixed(2)} / role ${item.role_affinity.toFixed(2)} / familiarity ${item.familiarity.toFixed(2)}</p>
                        </article>
                      `
                    )
                    .join("")
                : "<p class='muted'>尚无话题索引。</p>"
            }
          </div>

          <div class="detail-section">
            <h5>最近决策</h5>
            ${
              decisions.length
                ? decisions
                    .map(
                      (item) => `
                        <article class="decision-card">
                          <div class="timeline-row">
                            <strong>${decisionLabel(item.outcome)}</strong>
                            <span>${item.score.toFixed(2)} / ${item.threshold.toFixed(2)}</span>
                          </div>
                          <p>intent: ${item.reply_intent}</p>
                          <p class="muted">${(item.reason_codes_json ?? []).join(" / ") || "无原因码"}</p>
                        </article>
                      `
                    )
                    .join("")
                : "<p class='muted'>尚无决策记录。</p>"
            }
          </div>
        </section>
      `;
    })
    .join("");
}

function renderSnapshotsTab() {
  const analysis = state.messageAnalysis;
  if (!analysis || !analysis.snapshots.length) {
    elements.analysisContent.innerHTML = `
      <div class="empty-state">
        <h4>尚无上下文快照</h4>
        <p>该消息尚未触发可展示的快照。</p>
      </div>
    `;
    return;
  }

  const groupedItems = new Map();
  analysis.snapshot_items.forEach((item) => {
    const list = groupedItems.get(item.snapshot_id) ?? [];
    list.push(item);
    groupedItems.set(item.snapshot_id, list);
  });

  elements.analysisContent.innerHTML = analysis.snapshots
    .map((snapshot) => {
      const items = groupedItems.get(snapshot.id) ?? [];
      return `
        <section class="snapshot-card">
          <div class="timeline-row">
            <strong>${agentName(snapshot.agent_id)} / ${snapshot.purpose}</strong>
            <span>${snapshot.estimated_tokens} tokens</span>
          </div>
          <p class="muted">tokenizer: ${snapshot.tokenizer}，created ${longTime(snapshot.created_at)}</p>
          ${
            items.length
              ? items
                  .map(
                    (item) => `
                      <article class="snapshot-item">
                        <div class="timeline-row">
                          <strong>${item.item_type}</strong>
                          <span>${item.estimated_tokens} tokens</span>
                        </div>
                        <p>${escapeHtml(item.rendered_content)}</p>
                        <p class="muted">原因：${item.inclusion_reason}</p>
                      </article>
                    `
                  )
                  .join("")
              : "<p class='muted'>暂无快照条目。</p>"
          }
        </section>
      `;
    })
    .join("");
}

function renderCallsTab() {
  const analysis = state.messageAnalysis;
  if (!analysis || !analysis.model_calls.length) {
    elements.analysisContent.innerHTML = `
      <div class="empty-state">
        <h4>尚无模型调用记录</h4>
        <p>当前消息没有关联的模型调用。</p>
      </div>
    `;
    return;
  }

  elements.analysisContent.innerHTML = analysis.model_calls
    .map(
      (call) => `
        <article class="call-card">
          <div class="timeline-row">
            <strong>${call.purpose}</strong>
            <span>${call.status}</span>
          </div>
          <p>${call.provider} / ${call.model}${call.agent_id ? ` / ${agentName(call.agent_id)}` : ""}</p>
          <p class="muted">
            latency ${call.latency_ms} ms，input ${call.input_tokens}，output ${call.output_tokens}，cached ${call.cached_tokens}
          </p>
          <p class="muted">开始 ${longTime(call.started_at)}</p>
        </article>
      `
    )
    .join("");
}

function renderAll() {
  renderHealth();
  renderConversationList();
  renderWorkspace();
  renderMessages();
  renderComposer();
  renderAnalysis();
  renderStatusBar();
}

function topicTitle(topicId) {
  return state.topics.find((item) => item.id === topicId)?.title ?? topicId;
}

function agentName(agentId) {
  return participantMap().get(agentId)?.display_name ?? agentId;
}

function decisionLabel(outcome) {
  const labels = {
    selected: "选中发言",
    eligible_not_selected: "想回应但未选中",
    silent: "沉默",
    ineligible: "不具资格",
  };
  return labels[outcome] ?? outcome;
}

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

elements.apiBase.addEventListener("change", async () => {
  state.apiBase = getApiBase();
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

  const topicButton = event.target.closest("[data-topic-id]");
  if (topicButton) {
    selectTopic(topicButton.dataset.topicId);
  }
});

elements.analysisTabs.forEach((button) => {
  button.addEventListener("click", () => setActiveTab(button.dataset.tab));
});
elements.mobileTabs.forEach((button) => {
  button.addEventListener("click", () => setMobilePane(button.dataset.pane));
});

window.addEventListener("resize", renderWorkspace);

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
