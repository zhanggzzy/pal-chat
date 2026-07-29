import "./styles.css";

const elements = {
  apiBase: document.querySelector("#api-base"),
  refresh: document.querySelector("#refresh"),
  statusPill: document.querySelector("#status-pill"),
  generatedAt: document.querySelector("#generated-at"),
  appVersion: document.querySelector("#app-version"),
  bindHost: document.querySelector("#bind-host"),
  templateCount: document.querySelector("#template-count"),
  credentialCount: document.querySelector("#credential-count"),
  transitions: document.querySelector("#transitions"),
  modules: document.querySelector("#modules"),
  note: document.querySelector("#note"),
  conversationTitle: document.querySelector("#conversation-title"),
  conversationMeta: document.querySelector("#conversation-meta"),
  conversationState: document.querySelector("#conversation-state"),
  messageForm: document.querySelector("#message-form"),
  messageInput: document.querySelector("#message-input"),
  messages: document.querySelector("#messages"),
};

const state = {
  bootstrap: null,
  conversation: null,
  messages: [],
  latestSeq: 0,
  socket: null,
};

function apiBase() {
  return elements.apiBase.value.replace(/\/$/, "");
}

async function request(path, options = {}) {
  const response = await fetch(`${apiBase()}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers ?? {}) },
    ...options,
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    throw new Error(data?.error?.message ?? `${response.status} ${response.statusText}`);
  }
  return data;
}

function setStatus(label, tone) {
  elements.statusPill.textContent = label;
  elements.statusPill.className = `pill pill-${tone}`;
}

function formatTime(value) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderList(container, items, renderItem, emptyText) {
  if (!items.length) {
    container.innerHTML = `<p class="empty">${escapeHtml(emptyText)}</p>`;
    return;
  }
  container.innerHTML = items.map(renderItem).join("");
}

function renderBootstrap(payload) {
  state.bootstrap = payload;
  elements.generatedAt.textContent = `生成时间：${formatTime(payload.generated_at)}`;
  elements.appVersion.textContent = `${payload.app_name} ${payload.app_version}`;
  elements.bindHost.textContent = payload.bind_host;
  elements.templateCount.textContent = String(payload.templates.length);
  elements.credentialCount.textContent = String(payload.credentials.length);
  elements.note.textContent = payload.note ?? "阶段 2 已启用：公共消息与 CP 增量同步。";

  renderList(
    elements.transitions,
    payload.state_transitions,
    (item) =>
      `<span class="chip">${escapeHtml(item.from_status)} → ${escapeHtml(item.to_status)} · ${escapeHtml(item.via)}</span>`,
    "暂无状态转换。",
  );

  renderList(
    elements.modules,
    payload.module_registry.filter((item) =>
      ["message_sequence", "projection", "model_adapter", "delivery"].includes(item.kind),
    ),
    (item) => `
      <article class="card">
        <div class="card-top">
          <strong>${escapeHtml(item.module_id)}</strong>
          <span>${escapeHtml(item.protocol_family)}</span>
        </div>
        <p class="muted">能力：${escapeHtml(item.provides_capabilities.join(", ") || "无")}</p>
      </article>
    `,
    "暂无模块注册。",
  );
}

function renderConversation() {
  if (!state.conversation) {
    elements.conversationTitle.textContent = "未连接实验";
    elements.conversationMeta.textContent = "先刷新 bootstrap，再自动准备一个运行中的会话。";
    elements.conversationState.textContent = "-";
    return;
  }
  elements.conversationTitle.textContent = state.conversation.title;
  elements.conversationMeta.textContent = `ID ${state.conversation.id} · Profile Hash ${
    state.conversation.profile_hash ?? "未锁定"
  }`;
  elements.conversationState.textContent = state.conversation.status;
}

function upsertMessage(message, status = "sent") {
  const index = state.messages.findIndex(
    (item) =>
      item.message_id === message.message_id ||
      (message.client_message_id && item.client_message_id === message.client_message_id),
  );
  const merged = { ...message, ui_status: status };
  if (index >= 0) {
    state.messages[index] = { ...state.messages[index], ...merged };
  } else {
    state.messages.push(merged);
  }
  state.messages.sort((a, b) => {
    const left = a.conversation_seq ?? Number.MAX_SAFE_INTEGER;
    const right = b.conversation_seq ?? Number.MAX_SAFE_INTEGER;
    return left - right;
  });
  state.latestSeq = state.messages.reduce(
    (max, item) => Math.max(max, item.conversation_seq ?? 0),
    state.latestSeq,
  );
  renderMessages();
}

function renderMessages() {
  renderList(
    elements.messages,
    state.messages,
    (item) => `
      <article class="message ${escapeHtml(item.ui_status ?? "sent")}">
        <div class="message-top">
          <strong>#${escapeHtml(String(item.conversation_seq ?? "pending"))}</strong>
          <span>${escapeHtml(item.ui_status ?? "sent")}</span>
        </div>
        <p>${escapeHtml(item.content_markdown)}</p>
        <div class="message-meta">
          <span>${escapeHtml(item.sender_id ?? "user")}</span>
          <span>${item.committed_at ? escapeHtml(formatTime(item.committed_at)) : "待提交"}</span>
        </div>
      </article>
    `,
    "还没有公共消息。",
  );
}

function clientMessageId() {
  return `web-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
}

async function ensureConversation() {
  const list = await request("/api/v1/conversations");
  let conversation = list.items.find((item) => item.status === "running") ?? list.items[0] ?? null;
  if (!conversation) {
    conversation = await request("/api/v1/conversations", {
      method: "POST",
      body: JSON.stringify({ title: "Phase 2 Public Messages Demo" }),
    });
  }
  if (conversation.status === "draft") {
    await request(`/api/v1/conversations/${conversation.id}/validate`, { method: "POST" });
    conversation = await request(`/api/v1/conversations/${conversation.id}/start`, { method: "POST" });
  } else if (conversation.status === "validated" || conversation.status === "paused") {
    const action = conversation.status === "paused" ? "resume" : "start";
    conversation = await request(`/api/v1/conversations/${conversation.id}/${action}`, {
      method: "POST",
    });
  }
  state.conversation = conversation;
  renderConversation();
}

async function backfillMessages() {
  if (!state.conversation) {
    return;
  }
  const payload = await request(
    `/api/v1/conversations/${state.conversation.id}/messages?after_seq=${state.latestSeq}`,
  );
  for (const item of payload.items) {
    upsertMessage(item, "sent");
  }
}

function connectSocket() {
  if (!state.conversation) {
    return;
  }
  if (state.socket) {
    state.socket.close();
  }
  const url = new URL(`/ws/v1/conversations/${state.conversation.id}`, apiBase());
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("after_seq", String(state.latestSeq));
  const socket = new WebSocket(url);
  socket.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    if (payload.event_type === "session.snapshot") {
      state.latestSeq = Math.max(state.latestSeq, payload.payload.latest_conversation_seq);
      return;
    }
    if (payload.event_type === "message.committed") {
      const message = payload.payload.message;
      if (message.conversation_seq > state.latestSeq + 1) {
        void backfillMessages();
      }
      upsertMessage(message, "sent");
    }
  });
  socket.addEventListener("close", () => {
    window.setTimeout(() => {
      if (state.conversation) {
        connectSocket();
      }
    }, 1000);
  });
  state.socket = socket;
}

async function submitMessage(event) {
  event.preventDefault();
  if (!state.conversation) {
    return;
  }
  const content = elements.messageInput.value.trim();
  if (!content) {
    return;
  }
  const payload = {
    client_message_id: clientMessageId(),
    content_markdown: content,
    mentions: [],
    responds_to: [],
    primary_reply_to: null,
  };
  upsertMessage(
    {
      ...payload,
      message_id: payload.client_message_id,
      sender_id: "user",
      sender_kind: "user",
      conversation_seq: null,
      committed_at: null,
    },
    "pending",
  );
  elements.messageInput.value = "";
  try {
    const response = await request(`/api/v1/conversations/${state.conversation.id}/messages`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    upsertMessage(response.message, "sent");
  } catch (error) {
    upsertMessage(
      {
        ...payload,
        message_id: payload.client_message_id,
        sender_id: "user",
        sender_kind: "user",
      },
      "failed",
    );
    elements.note.textContent = error instanceof Error ? error.message : "消息提交失败";
  }
}

async function loadApp() {
  setStatus("连接中", "pending");
  try {
    const bootstrap = await request("/api/v1/bootstrap");
    renderBootstrap(bootstrap);
    await ensureConversation();
    await backfillMessages();
    connectSocket();
    setStatus("阶段 2 在线", "ok");
  } catch (error) {
    elements.note.textContent = error instanceof Error ? error.message : "加载失败";
    setStatus("连接失败", "error");
  }
}

elements.refresh.addEventListener("click", () => {
  void loadApp();
});
elements.messageForm.addEventListener("submit", (event) => {
  void submitMessage(event);
});

renderConversation();
renderMessages();
void loadApp();
