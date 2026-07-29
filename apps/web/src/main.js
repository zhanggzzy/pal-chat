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
  devCommand: document.querySelector("#dev-command"),
  buildCommand: document.querySelector("#build-command"),
  transitions: document.querySelector("#transitions"),
  modules: document.querySelector("#modules"),
  templates: document.querySelector("#templates"),
  credentials: document.querySelector("#credentials"),
  note: document.querySelector("#note"),
};

function apiBase() {
  return elements.apiBase.value.replace(/\/$/, "");
}

async function request(path) {
  const response = await fetch(`${apiBase()}${path}`);
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
    year: "numeric",
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
  elements.generatedAt.textContent = `生成时间：${formatTime(payload.generated_at)}`;
  elements.appVersion.textContent = `${payload.app_name} ${payload.app_version}`;
  elements.bindHost.textContent = payload.bind_host;
  elements.templateCount.textContent = String(payload.templates.length);
  elements.credentialCount.textContent = String(payload.credentials.length);
  elements.devCommand.textContent = payload.frontend.dev_command;
  elements.buildCommand.textContent = payload.frontend.build_command;
  if (payload.note) {
    elements.note.textContent = payload.note;
    elements.note.classList.remove("hidden");
  } else {
    elements.note.classList.add("hidden");
  }

  renderList(
    elements.transitions,
    payload.state_transitions,
    (item) =>
      `<span class="chip">${escapeHtml(item.from_status)} → ${escapeHtml(item.to_status)} · ${escapeHtml(item.via)}</span>`,
    "暂无状态转换。",
  );

  renderList(
    elements.modules,
    payload.module_registry,
    (item) => `
      <article class="card">
        <div class="card-top">
          <strong>${escapeHtml(item.module_id)}</strong>
          <span>${escapeHtml(item.implementation_version)}</span>
        </div>
        <p>${escapeHtml(item.kind)} / ${escapeHtml(item.protocol_family)}</p>
        <p class="muted">能力：${escapeHtml(item.provides_capabilities.join(", ") || "无")}</p>
      </article>
    `,
    "暂无模块注册。",
  );

  renderList(
    elements.templates,
    payload.templates,
    (item) => `
      <article class="card">
        <div class="card-top">
          <strong>${escapeHtml(item.title)}</strong>
          <span>${escapeHtml(item.slug)}</span>
        </div>
        <p>${escapeHtml(item.description || "无描述")}</p>
        <p class="muted">Profile：${escapeHtml(item.profile.title)} / ${escapeHtml(item.profile.prompt_version)}</p>
      </article>
    `,
    "暂无 Profile 模板。",
  );

  renderList(
    elements.credentials,
    payload.credentials,
    (item) => `
      <article class="card">
        <div class="card-top">
          <strong>${escapeHtml(item.label)}</strong>
          <span>${escapeHtml(item.status)}</span>
        </div>
        <p>${escapeHtml(item.provider)} · ${escapeHtml(item.masked_value)}</p>
        <p class="muted">${escapeHtml(item.credential_ref)}</p>
      </article>
    `,
    "当前未配置凭据。",
  );
}

async function loadBootstrap() {
  setStatus("连接中", "pending");
  try {
    const payload = await request("/api/v1/bootstrap");
    renderBootstrap(payload);
    setStatus("已连接", "ok");
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unknown error";
    setStatus("连接失败", "error");
    elements.note.textContent = message;
    elements.note.classList.remove("hidden");
  }
}

elements.refresh.addEventListener("click", () => {
  void loadBootstrap();
});

void loadBootstrap();
