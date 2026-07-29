from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModuleDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module_id: str
    implementation_version: str
    state_schema_version: int
    protocol_family: str
    kind: str
    config_schema: dict[str, object] = Field(default_factory=dict)
    requires_capabilities: list[str] = Field(default_factory=list)
    provides_capabilities: list[str] = Field(default_factory=list)
    conflicts_with: list[str] = Field(default_factory=list)
    supports_recovery: bool
    visualization_kinds: list[str] = Field(default_factory=list)


MODULE_REGISTRY: list[ModuleDescriptor] = [
    ModuleDescriptor(
        module_id="sequence.sqlite-append-only",
        implementation_version="1.0.0",
        state_schema_version=1,
        protocol_family="message-sequence-v1",
        kind="message_sequence",
        provides_capabilities=["atomic-commit", "append-only", "segment-chain"],
        supports_recovery=True,
        visualization_kinds=["raw-json"],
    ),
    ModuleDescriptor(
        module_id="projection.segment-chain",
        implementation_version="1.0.0",
        state_schema_version=1,
        protocol_family="projection-v1",
        kind="projection",
        requires_capabilities=["append-only"],
        provides_capabilities=["segment-chain", "budget-exhausted-fallback"],
        supports_recovery=True,
        visualization_kinds=["raw-json", "segment-list"],
    ),
    ModuleDescriptor(
        module_id="projection.fixed-window",
        implementation_version="1.0.0",
        state_schema_version=1,
        protocol_family="projection-v1",
        kind="projection",
        config_schema={"window_size": "int"},
        requires_capabilities=["append-only"],
        provides_capabilities=["segment-chain", "budget-exhausted-fallback"],
        supports_recovery=True,
        visualization_kinds=["raw-json", "segment-list"],
    ),
    ModuleDescriptor(
        module_id="trigger.debounced-observation",
        implementation_version="1.0.0",
        state_schema_version=1,
        protocol_family="trigger-v1",
        kind="trigger",
        provides_capabilities=["observation-batch"],
        supports_recovery=True,
        visualization_kinds=["raw-json"],
    ),
    ModuleDescriptor(
        module_id="context.simple",
        implementation_version="1.0.0",
        state_schema_version=1,
        protocol_family="context-bundle-v1",
        kind="context_assembly",
        requires_capabilities=["graph-memory", "segment-chain"],
        provides_capabilities=["decision-context", "action-context"],
        supports_recovery=True,
        visualization_kinds=["raw-json", "context-list"],
    ),
    ModuleDescriptor(
        module_id="memory.graph-overlay",
        implementation_version="1.0.0",
        state_schema_version=1,
        protocol_family="memory-v1",
        kind="memory",
        provides_capabilities=["graph-memory", "overlay-inspect"],
        supports_recovery=True,
        visualization_kinds=["raw-json"],
    ),
    ModuleDescriptor(
        module_id="decision.score-threshold",
        implementation_version="1.0.0",
        state_schema_version=1,
        protocol_family="decision-v1",
        kind="decision",
        requires_capabilities=["decision-context"],
        provides_capabilities=["structured-decision"],
        supports_recovery=True,
        visualization_kinds=["raw-json"],
    ),
    ModuleDescriptor(
        module_id="reconsideration.rules-first",
        implementation_version="1.0.0",
        state_schema_version=1,
        protocol_family="reconsideration-v1",
        kind="reconsideration",
        provides_capabilities=["stale-check"],
        supports_recovery=True,
        visualization_kinds=["raw-json"],
    ),
    ModuleDescriptor(
        module_id="timing.humanized",
        implementation_version="1.0.0",
        state_schema_version=1,
        protocol_family="timing-v1",
        kind="timing",
        provides_capabilities=["typing-delay"],
        supports_recovery=True,
        visualization_kinds=["raw-json"],
    ),
    ModuleDescriptor(
        module_id="delivery.broadcast-all",
        implementation_version="1.0.0",
        state_schema_version=1,
        protocol_family="delivery-v1",
        kind="delivery",
        provides_capabilities=["at-least-once-delivery"],
        supports_recovery=True,
        visualization_kinds=["raw-json"],
    ),
    ModuleDescriptor(
        module_id="budget.causal-episode-4-2-2",
        implementation_version="1.0.0",
        state_schema_version=1,
        protocol_family="budget-v1",
        kind="budget",
        provides_capabilities=["episode-budget"],
        supports_recovery=True,
        visualization_kinds=["raw-json"],
    ),
    ModuleDescriptor(
        module_id="model.scripted",
        implementation_version="1.0.0",
        state_schema_version=1,
        protocol_family="model-adapter-v1",
        kind="model_adapter",
        config_schema={"script": "list[ScriptedStep]"},
        provides_capabilities=["structured-output", "scripted-adapter"],
        supports_recovery=True,
        visualization_kinds=["raw-json"],
    ),
    ModuleDescriptor(
        module_id="model.openai-compatible",
        implementation_version="1.0.0",
        state_schema_version=1,
        protocol_family="model-adapter-v1",
        kind="model_adapter",
        config_schema={"base_url": "str", "credential_ref": "str"},
        provides_capabilities=["structured-output"],
        supports_recovery=True,
        visualization_kinds=["raw-json"],
    ),
]


DEFAULT_TEMPLATE_ID = "default-natural-chat-v1"


DEFAULT_MODULE_SELECTIONS: dict[str, str] = {
    "message_sequence": "sequence.sqlite-append-only",
    "projection": "projection.segment-chain",
    "trigger": "trigger.debounced-observation",
    "context_assembly": "context.simple",
    "memory": "memory.graph-overlay",
    "decision": "decision.score-threshold",
    "reconsideration": "reconsideration.rules-first",
    "timing": "timing.humanized",
    "delivery": "delivery.broadcast-all",
    "budget": "budget.causal-episode-4-2-2",
    "model_adapter": "model.scripted",
}


def registry_by_id() -> dict[str, ModuleDescriptor]:
    return {item.module_id: item for item in MODULE_REGISTRY}
