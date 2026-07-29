import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

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

const conversation = {
  id: "conv-1",
  title: "Monitoring",
  status: "running",
  draft_profile: {
    title: "Monitoring",
    prompt_version: "phase5",
    modules: {},
    agent_a: {
      agent_id: "agent-a",
      display_name: "A",
      persona_prompt: "",
      attention_prior: "A prior",
      response_threshold: 0.5,
      model: { provider: "scripted", model: "script" },
    },
    agent_b: {
      agent_id: "agent-b",
      display_name: "B",
      persona_prompt: "",
      attention_prior: "B prior",
      response_threshold: 0.5,
      model: { provider: "scripted", model: "script" },
    },
  },
  locked_profile: null,
  profile_hash: null,
  guardrails: {
    max_llm_calls: 32,
    max_total_tokens: 64000,
    max_auto_retries: 2,
    worker_restart_limit: 1,
    pause: false,
    stop: false,
    log_level: "info",
  },
  created_at: "2026-07-29T10:00:00Z",
  updated_at: "2026-07-29T10:00:00Z",
  ended_at: null,
};

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("App", () => {
  beforeEach(() => {
    MockSocket.instances.length = 0;
    vi.stubGlobal("WebSocket", MockSocket as unknown as typeof WebSocket);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/v1/conversations") && (!init || init.method === undefined)) {
          return jsonResponse({ items: [conversation] });
        }
        if (url.endsWith("/api/v1/bootstrap")) {
          return jsonResponse({
            app_name: "pal-chat",
            app_version: "0.5.0",
            generated_at: "2026-07-29T10:00:00Z",
            bind_host: "127.0.0.1",
          });
        }
        if (url.endsWith("/api/v1/conversations/conv-1")) {
          return jsonResponse({ conversation });
        }
        if (url.endsWith("/messages")) {
          return jsonResponse({ items: [] });
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
        if (url.includes("/memory/revisions")) {
          return jsonResponse({
            items: [{ revision: "mem-0", counter: 0, module_id: "memory.unknown", payload: {}, committed_at: null, bundle_revisions: [] }],
          });
        }
        if (url.includes("/context-bundles")) {
          return jsonResponse({ items: [] });
        }
        if (url.includes("/attempts")) {
          return jsonResponse({ items: [] });
        }
        throw new Error(`Unhandled fetch ${url}`);
      }),
    );
  });

  it("blocks writes in review mode and exposes raw fallback / accessibility affordances", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByText("Monitoring")).toBeInTheDocument());

    expect(screen.getByText("费用估算")).toBeInTheDocument();
    expect(screen.getByText("估算/解释性数据，非账单")).toBeInTheDocument();
    expect(screen.queryByText(/^Total Cost$/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("A 私有视图"));
    await waitFor(() => expect(screen.getByText("Raw Memory JSON")).toBeInTheDocument());

    fireEvent.click(screen.getByText("A 私有视图"));
    fireEvent.click(screen.getByText("Server 公共视图"));
    fireEvent.click(screen.getByText("A 私有视图"));
    fireEvent.click(screen.getByText("mem-0"));
    expect(screen.getByText("回看模式")).toBeInTheDocument();

    const textarea = screen.getByLabelText("Public Message");
    expect(textarea).toBeDisabled();
    expect(screen.getByRole("tablist", { name: "Inspector Tabs" })).toBeInTheDocument();
    expect(screen.getByRole("log")).toBeInTheDocument();
  });
});
