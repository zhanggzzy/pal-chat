export interface ValidationIssue {
  code: string;
  message: string;
  path: string;
}

export interface ValidationResult {
  ok: boolean;
  issues: ValidationIssue[];
}

export interface CredentialRead {
  credential_ref: string;
  provider: string;
  label: string;
  masked_value: string;
  status: string;
  created_at: string;
  updated_at: string;
  validated_at: string | null;
}

export interface ModuleSelection {
  module_id: string;
  config: Record<string, unknown>;
}

export interface ModelBinding {
  provider: string;
  model: string;
  credential_ref?: string | null;
  options?: Record<string, unknown>;
}

export interface AgentProfile {
  agent_id: string;
  display_name: string;
  persona_prompt: string;
  attention_prior: string;
  response_threshold: number;
  model: ModelBinding;
}

export interface ExperimentProfile {
  schema_version?: number;
  template_id: string;
  title: string;
  prompt_version: string;
  modules: Record<string, ModuleSelection>;
  agent_a: AgentProfile;
  agent_b: AgentProfile;
  metadata?: Record<string, unknown>;
}

export interface ProfileTemplateRead {
  id: string;
  slug: string;
  title: string;
  description: string;
  profile: ExperimentProfile;
  created_at: string;
  updated_at: string;
}

export interface TransitionRule {
  from_status: string;
  to_status: string;
  via: string;
}

export interface BootstrapResponse {
  app_name: string;
  app_version: string;
  generated_at: string;
  bind_host: string;
  module_registry: Array<Record<string, unknown>>;
  templates: ProfileTemplateRead[];
  credentials: CredentialRead[];
  state_transitions: TransitionRule[];
  frontend: Record<string, string>;
  note?: string | null;
}

export interface RuntimeGuardrails {
  max_llm_calls: number;
  max_total_tokens: number;
  max_auto_retries: number;
  worker_restart_limit: number;
  pause: boolean;
  stop: boolean;
  log_level: string;
}

export interface ConversationRead {
  id: string;
  title: string;
  status: string;
  draft_profile: ExperimentProfile;
  locked_profile: ExperimentProfile | null;
  profile_hash: string | null;
  guardrails: RuntimeGuardrails;
  catalog_metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  validated_at: string | null;
  started_at: string | null;
  ended_at: string | null;
  archive_dir: string | null;
  manifest_path: string | null;
}

export interface RuntimeAuthorityAgentState {
  worker_state: string;
  active_run_id: string | null;
  typing_status: string;
  typing_run_id: string | null;
  reliable_seq: number;
  dirty_since_seq: number | null;
  updated_at: string;
}

export interface RuntimeAuthority {
  latest_reliable_seq: number;
  agents: Record<string, RuntimeAuthorityAgentState>;
}

export interface ConversationDetail {
  conversation: ConversationRead;
  validation: ValidationResult;
  manifest: Record<string, unknown> | null;
  runtime_authority: RuntimeAuthority | null;
}

export interface Message {
  message_id: string;
  conversation_seq: number;
  sender_kind: string;
  sender_id: string;
  content_markdown: string;
  mentions: string[];
  primary_reply_to: string | null;
  responds_to: string[];
  client_message_id: string | null;
  causal_episode_id: string | null;
  caused_by_message_id: string | null;
  agent_hop: number;
  committed_at: string;
  cp_revision: number;
  ui_status?: "pending" | "sent" | "failed";
}

export interface CpRevision {
  projection_revision: number;
  covered_through_seq: number;
  strategy_id: string;
  strategy_version: string;
  snapshot: {
    segments: CpSegment[];
    last_operation?: string | null;
  };
  trace_ref: string;
  created_at: string;
}

export interface CpSegment {
  segment_id: string;
  ordinal: number;
  status: string;
  message_refs: string[];
  start_seq: number | null;
  end_seq: number | null;
  title: string;
  summary: string;
  base_activation: number;
  activation_updated_at: string;
  created_revision: number;
  closed_revision: number | null;
}

export interface RunAttempt {
  attempt_id: string;
  run_id: string;
  agent_id: string;
  phase: string;
  provider: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
  latency_ms: number | null;
  bundle_revision: string | null;
  memory_revision_before: string | null;
  memory_revision_after: string | null;
  staged_memory_revision: string | null;
  payload: Record<string, unknown> | null;
  created_at: string;
}

export interface AgentRun {
  run_id: string;
  agent_id: string;
  status: string;
  phase: string;
  observation_message_ids: string[];
  root_message_ids: string[];
  expected_conversation_seq: number;
  profile_hash: string;
  idempotency_key: string;
  causal_episode_id: string | null;
  caused_by_message_id: string | null;
  agent_hop: number;
  decision_json: Record<string, unknown> | null;
  draft_message_json: Record<string, unknown> | null;
  error_code: string | null;
  error_message: string | null;
  started_at: string;
  updated_at: string;
  finished_at: string | null;
  latency_ms: number | null;
  attempts: RunAttempt[];
}

export interface MemoryRevision {
  revision: string;
  counter: number;
  module_id: string;
  payload: Record<string, unknown>;
  committed_at: string | null;
  run_id?: string | null;
  conversation_seq?: number | null;
  cp_revision?: number | null;
  bundle_revisions: string[];
}

export interface ContextBundle {
  bundle_id: string;
  revision: string;
  phase: string;
  conversation_seq: number;
  projection_revision: number;
  memory_revision: string;
  messages: Message[];
  selected_public_refs: string[];
  selected_private_refs: string[];
  selection_trace: string[];
  observation_message_ids: string[];
  estimated_tokens: number;
  prompt_versions: Record<string, string>;
  rendered_items: string[];
  as_of_time: string;
}

export interface LogEntry {
  id: string;
  timestamp: string;
  level: string;
  phase: string | null;
  message: string;
  agent_id: string | null;
  run_id: string | null;
  attempt_id: string | null;
  details: Record<string, unknown> | null;
}

export interface CausalEpisode {
  episode_id: string;
  root_message_ids: string[];
  total_actions: number;
  agent_a_actions: number;
  agent_b_actions: number;
  max_agent_hop: number;
  updated_at: string;
}

export interface CostBreakdown {
  total_tokens: number;
  total_cost_usd: number;
  by_agent: Record<string, { tokens: number; cost_usd: number }>;
  by_phase: Record<string, { tokens: number; cost_usd: number }>;
}

export interface OutboxEvent {
  event_id: string;
  event_type: string;
  conversation_id: string;
  conversation_seq: number | null;
  emitted_at?: string | null;
  payload: Record<string, unknown>;
}

export interface SessionSnapshot {
  event_type: "session.snapshot";
  conversation_id: string;
  payload: {
    latest_conversation_seq: number;
    latest_cp_revision: number;
    cp_snapshot: CpRevision["snapshot"];
  };
}

export interface ReviewState {
  kind: "cp" | "memory";
  label: string;
  cpRevision?: number;
  agentId?: string;
  memoryRevision?: string;
}

export interface AutomaticMetrics {
  schema_version: number;
  conversation_id: string;
  computed_at: string;
  metrics: Record<string, number | string | null>;
}

export interface ManualScoreItem {
  criterion: string;
  score: number | null;
  note: string;
}

export interface ManualScore {
  schema_version: number;
  conversation_id: string;
  rubric_version: string;
  updated_at: string | null;
  scores: ManualScoreItem[];
  overall_score: number | null;
  overall_note: string;
}

export interface HistoryEntry {
  conversation_id: string;
  title: string | null;
  status: string | null;
  created_at: string | null;
  ended_at: string | null;
  profile_hash: string | null;
  adapter_module_id: string | null;
  adapter_known: boolean;
}

export interface HistoryDetail {
  entry: HistoryEntry;
  raw_manifest: Record<string, unknown>;
  messages: Record<string, unknown>[];
  cp_revisions: Record<string, unknown>[];
  logs: Record<string, unknown>[];
}

export interface AnalysisExportJob {
  job_id: string;
  conversation_id: string;
  status: string;
  created_at: string;
  updated_at: string;
  error: string | null;
  download_url: string | null;
}
