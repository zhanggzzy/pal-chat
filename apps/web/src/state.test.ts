import { describe, expect, it } from "vitest";

import { applySocketPayload, emptyRuntimeViewState, inferRunForMessage, upsertMessage } from "./state";
import type { AgentRun, Message, OutboxEvent } from "./types";

const baseMessage: Message = {
  message_id: "msg-1",
  conversation_seq: 1,
  sender_kind: "user",
  sender_id: "user",
  content_markdown: "hello",
  mentions: [],
  primary_reply_to: null,
  responds_to: [],
  client_message_id: "client-1",
  causal_episode_id: "episode-1",
  caused_by_message_id: null,
  agent_hop: 0,
  committed_at: "2026-07-29T10:00:00Z",
  cp_revision: 1,
};

describe("state helpers", () => {
  it("dedupes optimistic messages by client id", () => {
    const pending = upsertMessage([], { ...baseMessage, ui_status: "pending" });
    const committed = upsertMessage(pending, { ...baseMessage, message_id: "server-1", ui_status: "sent" });
    expect(committed).toHaveLength(1);
    expect(committed[0].message_id).toBe("server-1");
    expect(committed[0].ui_status).toBe("sent");
  });

  it("dedupes socket events by event id and freezes writes in review mode", () => {
    const event: OutboxEvent = {
      event_id: "evt-1",
      event_type: "message.committed",
      conversation_id: "conv-1",
      conversation_seq: 1,
      payload: { message: baseMessage },
    };
    const once = applySocketPayload(emptyRuntimeViewState(), event, null);
    const twice = applySocketPayload(once, event, null);
    expect(twice.messages).toHaveLength(1);

    const blocked = applySocketPayload(
      emptyRuntimeViewState(),
      {
        ...event,
        event_id: "evt-2",
      },
      { kind: "cp", label: "CP revision 1", cpRevision: 1 },
    );
    expect(blocked.messages).toHaveLength(0);
  });

  it("links agent messages back to the matching run", () => {
    const run: AgentRun = {
      run_id: "run-1",
      agent_id: "agent-a",
      status: "COMMITTED",
      phase: "SUBMITTING",
      observation_message_ids: [],
      root_message_ids: [],
      expected_conversation_seq: 1,
      profile_hash: "hash",
      idempotency_key: "run-1",
      causal_episode_id: "episode-1",
      caused_by_message_id: "msg-1",
      agent_hop: 1,
      decision_json: null,
      draft_message_json: null,
      error_code: null,
      error_message: null,
      started_at: "2026-07-29T10:00:00Z",
      updated_at: "2026-07-29T10:00:01Z",
      finished_at: "2026-07-29T10:00:02Z",
      latency_ms: 2000,
      attempts: [],
    };
    const linked = inferRunForMessage([run], {
      ...baseMessage,
      sender_kind: "agent",
      sender_id: "agent-a",
      agent_hop: 1,
    });
    expect(linked?.run_id).toBe("run-1");
  });
});
