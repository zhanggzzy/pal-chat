import type {
  AgentRun,
  CpRevision,
  Message,
  OutboxEvent,
  ReviewState,
  RuntimeGuardrails,
  SessionSnapshot,
} from "./types";

export interface RuntimeViewState {
  messages: Message[];
  cpRevisions: CpRevision[];
  runs: AgentRun[];
  latestSeq: number;
  latestCpRevision: number;
  guardrails: RuntimeGuardrails | null;
  typing: Record<string, { active: boolean; runId: string | null; reason?: string }>;
  seenEventIds: string[];
}

export const emptyRuntimeViewState = (): RuntimeViewState => ({
  messages: [],
  cpRevisions: [],
  runs: [],
  latestSeq: 0,
  latestCpRevision: 0,
  guardrails: null,
  typing: {},
  seenEventIds: [],
});

export function upsertMessage(messages: Message[], incoming: Message): Message[] {
  const next = [...messages];
  const index = next.findIndex(
    (item) =>
      item.message_id === incoming.message_id ||
      (incoming.client_message_id !== null && item.client_message_id === incoming.client_message_id),
  );
  const merged: Message = { ...incoming, ui_status: incoming.ui_status ?? "sent" };
  if (index >= 0) {
    next[index] = { ...next[index], ...merged };
  } else {
    next.push(merged);
  }
  next.sort((left, right) => left.conversation_seq - right.conversation_seq);
  return next;
}

export function markOptimisticStatus(
  messages: Message[],
  clientMessageId: string,
  status: Message["ui_status"],
): Message[] {
  return messages.map((message) =>
    message.client_message_id === clientMessageId ? { ...message, ui_status: status } : message,
  );
}

export function applySocketPayload(
  state: RuntimeViewState,
  event: OutboxEvent | SessionSnapshot,
  reviewState: ReviewState | null,
): RuntimeViewState {
  if ("event_type" in event && event.event_type === "session.snapshot") {
    return {
      ...state,
      latestSeq: Math.max(state.latestSeq, Number(event.payload.latest_conversation_seq)),
      latestCpRevision: Math.max(
        state.latestCpRevision,
        Number(event.payload.latest_cp_revision),
      ),
    };
  }
  const socketEvent = event as OutboxEvent;

  if (state.seenEventIds.includes(socketEvent.event_id)) {
    return state;
  }

  if (reviewState !== null) {
    return {
      ...state,
      seenEventIds: [...state.seenEventIds, socketEvent.event_id].slice(-400),
    };
  }

  const seenEventIds = [...state.seenEventIds, socketEvent.event_id].slice(-400);
  switch (socketEvent.event_type) {
    case "message.committed": {
      const message = socketEvent.payload.message as Message;
      return {
        ...state,
        seenEventIds,
        latestSeq: Math.max(state.latestSeq, message.conversation_seq),
        messages: upsertMessage(state.messages, { ...message, ui_status: "sent" }),
      };
    }
    case "cp.revision_committed": {
      const cpRevision = socketEvent.payload.cp_revision as CpRevision;
      const others = state.cpRevisions.filter(
        (item) => item.projection_revision !== cpRevision.projection_revision,
      );
      return {
        ...state,
        seenEventIds,
        latestCpRevision: Math.max(state.latestCpRevision, cpRevision.projection_revision),
        cpRevisions: [...others, cpRevision].sort(
          (left, right) => left.projection_revision - right.projection_revision,
        ),
      };
    }
    case "submission.status_changed": {
      const clientMessageId = String(socketEvent.payload.client_message_id ?? "");
      const status = socketEvent.payload.status === "failed" ? "failed" : "sent";
      return {
        ...state,
        seenEventIds,
        messages: markOptimisticStatus(state.messages, clientMessageId, status),
      };
    }
    case "agent.typing_started":
    case "agent.typing_stopped": {
      const agentId = String(socketEvent.payload.agent_id ?? "");
      return {
        ...state,
        seenEventIds,
        typing: {
          ...state.typing,
          [agentId]: {
            active: socketEvent.event_type === "agent.typing_started",
            runId: (socketEvent.payload.run_id as string | null | undefined) ?? null,
            reason: (socketEvent.payload.reason as string | undefined) ?? undefined,
          },
        },
      };
    }
    case "agent.run_updated": {
      const payload = socketEvent.payload as unknown as Partial<AgentRun> & {
        run_id: string;
        agent_id: string;
      };
      const runs = [...state.runs];
      const index = runs.findIndex((item) => item.run_id === payload.run_id);
      if (index >= 0) {
        runs[index] = { ...runs[index], ...payload };
      }
      return {
        ...state,
        seenEventIds,
        runs,
      };
    }
    case "guardrails.changed": {
      return {
        ...state,
        seenEventIds,
        guardrails:
          (socketEvent.payload.guardrails as RuntimeGuardrails | undefined) ?? state.guardrails,
      };
    }
    default:
      return { ...state, seenEventIds };
  }
}

export function inferRunForMessage(runs: AgentRun[], message: Message): AgentRun | null {
  return (
    runs.find(
      (run) =>
        run.agent_id === message.sender_id &&
        run.causal_episode_id === message.causal_episode_id &&
        run.agent_hop === message.agent_hop,
    ) ??
    runs.find((run) => run.agent_id === message.sender_id) ??
    null
  );
}
