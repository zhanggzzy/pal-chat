import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type { ConversationRead, CredentialRead, ExperimentProfile, Message } from "./types";

class MockSocket {
  static instances: MockSocket[] = [];

  public onopen: (() => void) | null = null;
  public onmessage: ((event: MessageEvent<string>) => void) | null = null;
  public onclose: (() => void) | null = null;

  constructor(public readonly url: string) {
    MockSocket.instances.push(this);
    queueMicrotask(() => this.onopen?.());
  }

  addEventListener(type: string, handler: EventListener): void {
    if (type === "open") {
      this.onopen = handler as () => void;
    }
    if (type === "message") {
      this.onmessage = handler as (event: MessageEvent<string>) => void;
    }
    if (type === "close") {
      this.onclose = handler as () => void;
    }
  }

  close(): void {
    this.onclose?.();
  }
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeProfile(): ExperimentProfile {
  return {
    schema_version: 1,
    template_id: "tpl-1",
    title: "实验草稿",
    prompt_version: "phase1",
    modules: {
      model_adapter: { module_id: "model.scripted", config: {} },
      message_sequence: { module_id: "message.sequence", config: {} },
      projection: { module_id: "projection.default", config: {} },
    },
    agent_a: {
      agent_id: "agent-a",
      display_name: "Agent A",
      persona_prompt: "A persona",
      attention_prior: "A prior",
      response_threshold: 0.55,
      model: { provider: "scripted", model: "scripted://phase1", credential_ref: null },
    },
    agent_b: {
      agent_id: "agent-b",
      display_name: "Agent B",
      persona_prompt: "B persona",
      attention_prior: "B prior",
      response_threshold: 0.55,
      model: { provider: "scripted", model: "scripted://phase1", credential_ref: null },
    },
    metadata: {},
  };
}

function makeConversation(status: string): ConversationRead {
  return {
    id: "conv-1",
    title: "实验草稿",
    status,
    draft_profile: makeProfile(),
    locked_profile: status === "draft" ? null : makeProfile(),
    profile_hash: status === "draft" ? null : "hash-1",
    guardrails: {
      max_llm_calls: 32,
      max_total_tokens: 64000,
      max_auto_retries: 2,
      worker_restart_limit: 1,
      pause: false,
      stop: false,
      log_level: "info",
    },
    catalog_metadata: {},
    created_at: "2026-08-02T07:00:00Z",
    updated_at: "2026-08-02T07:00:00Z",
    validated_at: null,
    started_at: null,
    ended_at: null,
    archive_dir: null,
    manifest_path: null,
  };
}

describe("App", () => {
  let credentials: CredentialRead[];
  let conversation: ConversationRead;
  let historyConversation: ConversationRead;
  let messages: Message[];
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    credentials = [];
    conversation = makeConversation("draft");
    historyConversation = {
      ...makeConversation("ended"),
      id: "conv-ended",
      title: "旧实验",
      ended_at: "2026-08-02T07:30:00Z",
    };
    messages = [
      {
        message_id: "msg-1",
        conversation_seq: 1,
        sender_kind: "user",
        sender_id: "user",
        content_markdown: "第一条公共消息",
        mentions: [],
        primary_reply_to: null,
        responds_to: [],
        client_message_id: "seed-1",
        causal_episode_id: "ep-1",
        caused_by_message_id: null,
        agent_hop: 0,
        committed_at: "2026-08-02T07:05:00Z",
        cp_revision: 1,
      },
    ];

    MockSocket.instances.length = 0;
    vi.stubGlobal("WebSocket", MockSocket as unknown as typeof WebSocket);
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";

      if (url.endsWith("/api/v1/bootstrap")) {
        return jsonResponse({
          app_name: "pal-chat",
          app_version: "0.5.0",
          generated_at: "2026-08-02T07:00:00Z",
          bind_host: "127.0.0.1",
          module_registry: [],
          templates: [
            {
              id: "tpl-1",
              slug: "default-natural-chat-v1",
              title: "默认自然群聊",
              description: "测试模板",
              profile: makeProfile(),
              created_at: "2026-08-02T07:00:00Z",
              updated_at: "2026-08-02T07:00:00Z",
            },
          ],
          credentials,
          state_transitions: [],
          frontend: {},
          note: null,
        });
      }

      if (url.endsWith("/api/v1/history")) {
        return jsonResponse({
          items: [
            {
              conversation_id: historyConversation.id,
              title: historyConversation.title,
              status: historyConversation.status,
              created_at: historyConversation.created_at,
              ended_at: historyConversation.ended_at,
              profile_hash: historyConversation.profile_hash,
              adapter_module_id: "model.legacy-unknown",
              adapter_known: false,
            },
          ],
        });
      }

      if (url.endsWith("/api/v1/conversations") && method === "GET") {
        return jsonResponse({ items: [conversation, historyConversation] });
      }

      if (url.endsWith("/api/v1/conversations") && method === "POST") {
        const body = JSON.parse(String(init?.body)) as {
          title: string;
          draft_profile?: ExperimentProfile;
        };
        conversation = {
          ...makeConversation("draft"),
          title: body.title,
          draft_profile: body.draft_profile ?? makeProfile(),
          updated_at: "2026-08-02T07:08:00Z",
        };
        return jsonResponse(conversation, 201);
      }

      if (url.endsWith("/api/v1/profile-templates/tpl-1/clone")) {
        return jsonResponse({ profile: makeProfile() });
      }

      if (url.endsWith("/api/v1/conversations/conv-1/workbench")) {
        return jsonResponse({
          detail: {
            conversation,
            runtime_authority: {
              latest_reliable_seq: messages.at(-1)?.conversation_seq ?? 0,
              agents: {
                "agent-a": {
                  worker_state: "LISTENING",
                  active_run_id: null,
                  typing_status: "idle",
                  typing_run_id: null,
                  reliable_seq: 1,
                  dirty_since_seq: null,
                  updated_at: "2026-08-02T07:00:00Z",
                },
                "agent-b": {
                  worker_state: "LISTENING",
                  active_run_id: null,
                  typing_status: "idle",
                  typing_run_id: null,
                  reliable_seq: 1,
                  dirty_since_seq: null,
                  updated_at: "2026-08-02T07:00:00Z",
                },
              },
            },
          },
          messages,
        });
      }

      if (url.endsWith("/api/v1/conversations/conv-1") && method === "GET") {
        return jsonResponse({
          conversation,
          validation:
            conversation.status === "draft"
              ? {
                  ok: false,
                  issues: [{ code: "missing_credential_ref", path: "agent_a.model", message: "待校验" }],
                }
              : { ok: true, issues: [] },
          manifest: null,
          runtime_authority: null,
        });
      }

      if (url.endsWith("/api/v1/conversations/conv-1/validate")) {
        conversation = {
          ...conversation,
          status: "validated",
          validated_at: "2026-08-02T07:10:00Z",
        };
        return jsonResponse({ ok: true, issues: [] });
      }

      if (url.endsWith("/api/v1/conversations/conv-1/start")) {
        conversation = {
          ...conversation,
          status: "running",
          locked_profile: cloneProfile(conversation.draft_profile),
          profile_hash: "hash-1",
          archive_dir: "/tmp/archive",
          started_at: "2026-08-02T07:15:00Z",
        };
        return jsonResponse(conversation);
      }

      if (url.endsWith("/api/v1/conversations/conv-1/catalog-metadata")) {
        const body = JSON.parse(String(init?.body)) as { metadata: Record<string, unknown> };
        conversation = { ...conversation, catalog_metadata: body.metadata };
        return jsonResponse(conversation);
      }

      if (url.endsWith("/api/v1/conversations/conv-1/draft-profile")) {
        const body = JSON.parse(String(init?.body)) as { draft_profile: ExperimentProfile };
        conversation = { ...conversation, draft_profile: body.draft_profile };
        return jsonResponse(conversation);
      }

      if (url.endsWith("/api/v1/credentials") && method === "POST") {
        const body = JSON.parse(String(init?.body)) as { provider: string; label: string };
        const next: CredentialRead = {
          credential_ref: `cred-${credentials.length + 1}`,
          provider: body.provider,
          label: body.label,
          masked_value: "****1234",
          status: "created",
          created_at: "2026-08-02T07:00:00Z",
          updated_at: "2026-08-02T07:00:00Z",
          validated_at: null,
        };
        credentials = [...credentials, next];
        return jsonResponse({ credential: next, ok: true }, 201);
      }

      if (url.endsWith("/api/v1/credentials") && method === "GET") {
        return jsonResponse({ items: credentials });
      }

      if (url.includes("/api/v1/credentials/") && url.endsWith("/validate")) {
        credentials = credentials.map((item) =>
          item.credential_ref === "cred-1"
            ? { ...item, validated_at: "2026-08-02T07:20:00Z", status: "validated" }
            : item,
        );
        return jsonResponse({ credential: credentials[0], ok: true });
      }

      if (url.endsWith("/api/v1/conversations/conv-1/messages")) {
        const body = JSON.parse(String(init?.body)) as {
          client_message_id: string;
          content_markdown: string;
          mentions: string[];
          primary_reply_to: string | null;
          responds_to: string[];
        };
        if (body.content_markdown.includes("fail")) {
          return jsonResponse({ error: { message: "发送失败" }, request_id: "req-1" }, 500);
        }
        const next: Message = {
          message_id: `msg-${messages.length + 1}`,
          conversation_seq: messages.length + 1,
          sender_kind: "user",
          sender_id: "user",
          content_markdown: body.content_markdown,
          mentions: body.mentions,
          primary_reply_to: body.primary_reply_to,
          responds_to: body.responds_to,
          client_message_id: body.client_message_id,
          causal_episode_id: null,
          caused_by_message_id: null,
          agent_hop: 0,
          committed_at: "2026-08-02T07:22:00Z",
          cp_revision: 1,
        };
        messages = [...messages, next];
        return jsonResponse({ message: next });
      }

      if (url.includes("/submissions/") && url.endsWith("/retry")) {
        const next: Message = {
          message_id: "msg-retry",
          conversation_seq: messages.length + 1,
          sender_kind: "user",
          sender_id: "user",
          content_markdown: "重试已送达",
          mentions: [],
          primary_reply_to: null,
          responds_to: [],
          client_message_id: "web-retry",
          causal_episode_id: null,
          caused_by_message_id: null,
          agent_hop: 0,
          committed_at: "2026-08-02T07:25:00Z",
          cp_revision: 1,
        };
        messages = [...messages, next];
        return jsonResponse({ message: next });
      }

      if (url.endsWith("/cp-revisions")) {
        return jsonResponse({ items: [] });
      }
      if (url.endsWith("/runs")) {
        return jsonResponse({ items: [] });
      }
      if (url.endsWith("/logs")) {
        return jsonResponse({ items: [] });
      }
      if (url.endsWith("/causal-episodes")) {
        return jsonResponse({ items: [] });
      }
      if (url.endsWith("/metrics/cost-breakdown")) {
        return jsonResponse({ total_tokens: 0, total_cost_usd: 0, by_agent: {}, by_phase: {} });
      }
      if (url.endsWith("/metrics/automatic")) {
        return jsonResponse({
          schema_version: 1,
          conversation_id: "conv-1",
          computed_at: "2026-08-02T07:00:00Z",
          metrics: { message_count: messages.length },
        });
      }
      if (url.endsWith("/manual-score")) {
        return jsonResponse({
          schema_version: 1,
          conversation_id: "conv-1",
          rubric_version: "manual-score-v1",
          updated_at: "2026-08-02T07:00:00Z",
          scores: [],
          overall_score: null,
          overall_note: "",
        });
      }
      if (url.endsWith("/analysis-exports") && method === "POST") {
        return jsonResponse({
          job_id: "job-1",
          conversation_id: "conv-1",
          status: "ready",
          created_at: "2026-08-02T07:30:00Z",
          updated_at: "2026-08-02T07:30:00Z",
          error: null,
          download_url: "/api/v1/conversations/conv-1/analysis-exports/job-1/download",
        });
      }
      if (url.endsWith("/api/v1/history/conv-ended")) {
        return jsonResponse({
          entry: {
            conversation_id: "conv-ended",
            title: "旧实验",
            status: "ended",
            created_at: "2026-08-02T07:00:00Z",
            ended_at: "2026-08-02T07:30:00Z",
            profile_hash: "hash-old",
            adapter_module_id: "model.legacy-unknown",
            adapter_known: false,
          },
          raw_manifest: { catalog_metadata: { title_override: "旧实验" } },
          messages: [],
          cp_revisions: [],
          logs: [],
        });
      }
      if (url.includes("/memory/revisions")) {
        return jsonResponse({ items: [] });
      }
      if (url.includes("/context-bundles")) {
        return jsonResponse({ items: [] });
      }
      if (url.includes("/attempts")) {
        return jsonResponse({ items: [] });
      }

      throw new Error(`Unhandled fetch ${method} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("supports the 9-step wizard, explicit splitters, validation and start flow", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("群聊实验台")).toBeInTheDocument());
    expect(screen.getByRole("separator", { name: "主分隔线" })).toBeInTheDocument();
    expect(screen.getByRole("separator", { name: "次分隔线" })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Credential Label"), { target: { value: "OpenAI 主账号" } });
    fireEvent.change(screen.getByLabelText("Credential Secret"), { target: { value: "sk-test" } });
    fireEvent.submit(screen.getByLabelText("Credential Secret").closest("form") as HTMLFormElement);

    await waitFor(() => expect(screen.getByText("OpenAI 主账号")).toBeInTheDocument());

    fireEvent.click(screen.getByText("验证"));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/v1/credentials/cred-1/validate"),
        expect.objectContaining({ method: "POST" }),
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "打开 9 步向导" }));
    await waitFor(() => expect(screen.getByRole("dialog", { name: "新建实验向导" })).toBeInTheDocument());
    expect(screen.getByText("1. 模板与策略包")).toBeInTheDocument();
    expect(screen.getByText("9. 配置 Diff 与启动确认")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Wizard Draft Title"), { target: { value: "九步实验" } });
    fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    fireEvent.change(screen.getByLabelText("Wizard Agent A Name"), { target: { value: "策展人 A" } });
    fireEvent.change(screen.getByLabelText("Wizard Agent B Name"), { target: { value: "策展人 B" } });
    for (let index = 0; index < 7; index += 1) {
      fireEvent.click(screen.getByRole("button", { name: "下一步" }));
    }
    await waitFor(() => expect(screen.getByText("配置 Diff")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "创建草稿并校验" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "新建实验向导" })).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("heading", { name: "九步实验" })).toBeInTheDocument());

    fireEvent.click(screen.getByText("校验配置"));
    await waitFor(() => expect(screen.getByText("校验通过，可以开始实验")).toBeInTheDocument());

    fireEvent.click(screen.getByText("开始实验"));
    await waitFor(() => expect(screen.getByLabelText("Agent A Display Name")).toBeDisabled());

    fireEvent.change(screen.getByLabelText("Metadata Title"), { target: { value: "验收标题" } });
    fireEvent.change(screen.getByLabelText("Metadata Tags"), { target: { value: "scripted, 验收" } });
    fireEvent.click(screen.getByText("保存目录信息"));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/v1/conversations/conv-1/catalog-metadata"),
        expect.objectContaining({ method: "PATCH" }),
      ),
    );
  });

  it("supports mentions, quote send, retry and raw history fallback", async () => {
    conversation = makeConversation("running");
    conversation.locked_profile = cloneProfile(conversation.draft_profile);
    conversation.archive_dir = "/tmp/archive";

    render(<App />);
    await waitFor(() => expect(screen.getByText("第一条公共消息")).toBeInTheDocument());

    const textarea = screen.getByLabelText("Public Message");
    fireEvent.change(textarea, { target: { value: "@", selectionStart: 1 } });
    await waitFor(() => expect(screen.getByRole("listbox", { name: "Mention Suggestions" })).toBeInTheDocument());
    fireEvent.keyDown(textarea, { key: "ArrowDown" });
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(screen.getByLabelText("Public Message")).toHaveValue("@A ");

    fireEvent.click(screen.getByText("引用"));
    await waitFor(() => expect(screen.getByText(/引用：第一条公共消息/)).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Public Message"), {
      target: { value: "@A 请接着回答", selectionStart: 8 },
    });
    fireEvent.keyDown(screen.getByLabelText("Public Message"), { key: "Enter" });
    await waitFor(() => expect(screen.getByText("@A 请接着回答")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Public Message"), {
      target: { value: "fail once", selectionStart: 9 },
    });
    fireEvent.keyDown(screen.getByLabelText("Public Message"), { key: "Enter" });
    await waitFor(() => expect(screen.getByText("重试")).toBeInTheDocument());
    fireEvent.click(screen.getByText("重试"));
    await waitFor(() => expect(screen.getByText("重试已送达")).toBeInTheDocument());

    fireEvent.click(screen.getByText("旧实验"));
    await waitFor(() => expect(screen.getByText("历史档案 / Raw Fallback")).toBeInTheDocument());
    expect(screen.getByText("raw fallback")).toBeInTheDocument();
  });
});

function cloneProfile(profile: ExperimentProfile): ExperimentProfile {
  return JSON.parse(JSON.stringify(profile)) as ExperimentProfile;
}
