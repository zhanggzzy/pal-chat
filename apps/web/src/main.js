import "./styles.css";

const state = {
  currentConversationId: "",
};

const apiBaseInput = document.querySelector("#api-base");
const healthBadge = document.querySelector("#health-badge");
const liveStatus = document.querySelector("#live-status");
const readyStatus = document.querySelector("#ready-status");
const schemaStatus = document.querySelector("#schema-status");
const conversationBadge = document.querySelector("#conversation-badge");
const conversationIdInput = document.querySelector("#conversation-id");
const conversationOutput = document.querySelector("#conversation-output");
const messageBadge = document.querySelector("#message-badge");
const messageOutput = document.querySelector("#message-output");

function apiBase() {
  return apiBaseInput.value.replace(/\/$/, "");
}

function setBadge(node, tone, text) {
  node.className = `badge ${tone}`;
  node.textContent = text;
}

function renderOutput(node, payload) {
  node.textContent = JSON.stringify(payload, null, 2);
}

async function request(path, init = {}) {
  const response = await fetch(`${apiBase()}${path}`, {
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

async function checkHealth() {
  setBadge(healthBadge, "pending", "checking");
  try {
    const [live, ready] = await Promise.all([
      request("/health/live"),
      request("/health/ready"),
    ]);
    liveStatus.textContent = live.status;
    readyStatus.textContent = ready.status;
    schemaStatus.textContent = ready.schema_revision ?? "-";
    setBadge(healthBadge, "ok", "ready");
  } catch (error) {
    liveStatus.textContent = "error";
    readyStatus.textContent = "error";
    schemaStatus.textContent = "-";
    setBadge(healthBadge, "error", "offline");
    renderOutput(conversationOutput, { error: error.message });
  }
}

async function createConversation(event) {
  event.preventDefault();
  setBadge(conversationBadge, "pending", "creating");
  const title = document.querySelector("#conversation-title").value.trim();
  try {
    const data = await request("/api/v1/conversations", {
      method: "POST",
      body: JSON.stringify({ title }),
    });
    state.currentConversationId = data.conversation.id;
    conversationIdInput.value = state.currentConversationId;
    renderOutput(conversationOutput, data);
    setBadge(conversationBadge, "ok", "created");
  } catch (error) {
    setBadge(conversationBadge, "error", "failed");
    renderOutput(conversationOutput, { error: error.message });
  }
}

async function refreshConversation() {
  const conversationId = conversationIdInput.value.trim();
  if (!conversationId) {
    setBadge(conversationBadge, "error", "missing id");
    renderOutput(conversationOutput, { error: "Conversation ID is required." });
    return;
  }
  setBadge(conversationBadge, "pending", "loading");
  try {
    const data = await request(`/api/v1/conversations/${conversationId}`);
    state.currentConversationId = conversationId;
    renderOutput(conversationOutput, data);
    setBadge(conversationBadge, "ok", "loaded");
  } catch (error) {
    setBadge(conversationBadge, "error", "failed");
    renderOutput(conversationOutput, { error: error.message });
  }
}

async function sendMessage(event) {
  event.preventDefault();
  const conversationId = conversationIdInput.value.trim();
  const content = document.querySelector("#message-content").value.trim();
  if (!conversationId) {
    setBadge(messageBadge, "error", "missing id");
    renderOutput(messageOutput, { error: "Conversation ID is required." });
    return;
  }
  if (!content) {
    setBadge(messageBadge, "error", "missing text");
    renderOutput(messageOutput, { error: "Message content is required." });
    return;
  }
  setBadge(messageBadge, "pending", "sending");
  try {
    const data = await request(`/api/v1/conversations/${conversationId}/messages`, {
      method: "POST",
      headers: {
        "Idempotency-Key": `debug-${Date.now()}`,
      },
      body: JSON.stringify({ content }),
    });
    renderOutput(messageOutput, data);
    setBadge(messageBadge, "ok", data.status);
  } catch (error) {
    setBadge(messageBadge, "error", "failed");
    renderOutput(messageOutput, { error: error.message });
  }
}

async function refreshMessages() {
  const conversationId = conversationIdInput.value.trim();
  if (!conversationId) {
    setBadge(messageBadge, "error", "missing id");
    renderOutput(messageOutput, { error: "Conversation ID is required." });
    return;
  }
  setBadge(messageBadge, "pending", "loading");
  try {
    const data = await request(`/api/v1/conversations/${conversationId}/messages`);
    renderOutput(messageOutput, data);
    setBadge(messageBadge, "ok", `${data.items.length} messages`);
  } catch (error) {
    setBadge(messageBadge, "error", "failed");
    renderOutput(messageOutput, { error: error.message });
  }
}

document.querySelector("#check-health").addEventListener("click", checkHealth);
document.querySelector("#conversation-form").addEventListener("submit", createConversation);
document.querySelector("#refresh-conversation").addEventListener("click", refreshConversation);
document.querySelector("#message-form").addEventListener("submit", sendMessage);
document.querySelector("#refresh-messages").addEventListener("click", refreshMessages);

checkHealth();
