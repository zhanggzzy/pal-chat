from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha1
from pathlib import Path
from typing import Any


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return deepcopy(default)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else deepcopy(default)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _token_estimate(*parts: object) -> int:
    total_chars = 0
    for part in parts:
        total_chars += len(json.dumps(part, ensure_ascii=False, sort_keys=True))
    return max(1, total_chars // 4)


class PrivateRuntimeStore:
    def __init__(
        self,
        *,
        state_dir: Path,
        agent_id: str,
        attention_prior: str,
        prompt_version: str,
        memory_module_id: str,
        memory_config: dict[str, Any],
        context_module_id: str,
        context_config: dict[str, Any],
    ) -> None:
        self.state_dir = state_dir
        self.agent_id = agent_id
        self.attention_prior = attention_prior
        self.prompt_version = prompt_version
        self.memory_module_id = memory_module_id
        self.memory_config = memory_config
        self.context_module_id = context_module_id
        self.context_config = context_config
        self.memory_dir = self.state_dir / "memory"
        self.memory_revisions_dir = self.memory_dir / "revisions"
        self.memory_current_path = self.memory_dir / "current.json"
        self.context_dir = self.state_dir / "context-bundles"
        self.context_index_path = self.context_dir / "index.json"
        self._ensure_initialized()

    def _initial_memory_payload(self) -> dict[str, Any]:
        if self.memory_module_id == "memory.no-memory":
            return {
                "kind": "no-memory",
                "attention_prior": self.attention_prior,
                "nodes": [],
                "relations": [],
                "overlay": [],
                "applied_operations": [],
            }
        return {
            "kind": "graph-overlay",
            "attention_prior": self.attention_prior,
            "nodes": [],
            "relations": [],
            "overlay": [],
            "applied_operations": [],
        }

    def _ensure_initialized(self) -> None:
        self.memory_revisions_dir.mkdir(parents=True, exist_ok=True)
        self.context_dir.mkdir(parents=True, exist_ok=True)
        if self.memory_current_path.exists():
            return
        initial_payload = self._initial_memory_payload()
        base_record = {
            "revision": "mem-0",
            "counter": 0,
            "module_id": self.memory_module_id,
            "payload": initial_payload,
            "committed_at": None,
        }
        _write_json(self.memory_revisions_dir / "mem-0.json", base_record)
        _write_json(self.memory_current_path, base_record)
        _write_json(self.context_index_path, {"counter": 0})

    def snapshot(self) -> dict[str, Any]:
        current = _read_json(
            self.memory_current_path,
            {
                "revision": "mem-0",
                "counter": 0,
                "module_id": self.memory_module_id,
                "payload": self._initial_memory_payload(),
                "committed_at": None,
            },
        )
        return {
            "revision": str(current["revision"]),
            "counter": int(current.get("counter", 0)),
            "payload": deepcopy(current["payload"]),
            "module_id": str(current.get("module_id", self.memory_module_id)),
            "committed_at": current.get("committed_at"),
        }

    def stage_preview(
        self,
        *,
        operations: list[dict[str, Any]],
        run_id: str,
        phase: str,
        as_of_time: str,
    ) -> dict[str, Any] | None:
        if not operations:
            return None
        current = self.snapshot()
        if self.memory_module_id == "memory.no-memory":
            return {
                "revision": current["revision"],
                "base_revision": current["revision"],
                "payload": current["payload"],
                "operations": [],
                "phase": phase,
                "run_id": run_id,
                "as_of_time": as_of_time,
            }
        next_payload = deepcopy(current["payload"])
        self._apply_operations(
            next_payload,
            operations=operations,
            run_id=run_id,
            as_of_time=as_of_time,
        )
        digest = sha1(
            json.dumps(
                {
                    "base_revision": current["revision"],
                    "operations": operations,
                    "phase": phase,
                    "run_id": run_id,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:12]
        return {
            "revision": f"staged-{digest}",
            "base_revision": current["revision"],
            "payload": next_payload,
            "operations": deepcopy(operations),
            "phase": phase,
            "run_id": run_id,
            "as_of_time": as_of_time,
        }

    def commit_operations(
        self,
        *,
        operations: list[dict[str, Any]],
        run_id: str,
        conversation_seq: int,
        cp_revision: int,
        bundle_revisions: list[str],
        as_of_time: str,
    ) -> dict[str, Any]:
        current = self.snapshot()
        if not operations or self.memory_module_id == "memory.no-memory":
            return current
        next_payload = deepcopy(current["payload"])
        self._apply_operations(
            next_payload,
            operations=operations,
            run_id=run_id,
            as_of_time=as_of_time,
        )
        next_counter = int(current["counter"]) + 1
        next_revision = f"mem-{next_counter}"
        committed = {
            "revision": next_revision,
            "counter": next_counter,
            "module_id": self.memory_module_id,
            "payload": next_payload,
            "committed_at": as_of_time,
            "run_id": run_id,
            "conversation_seq": conversation_seq,
            "cp_revision": cp_revision,
            "bundle_revisions": list(bundle_revisions),
        }
        _write_json(self.memory_revisions_dir / f"{next_revision}.json", committed)
        _write_json(self.memory_current_path, committed)
        return {
            "revision": next_revision,
            "counter": next_counter,
            "payload": deepcopy(next_payload),
            "module_id": self.memory_module_id,
            "committed_at": as_of_time,
        }

    def build_context_bundle(
        self,
        *,
        phase: str,
        as_of_time: str,
        conversation_seq: int,
        projection_revision: int,
        cp_snapshot: dict[str, Any],
        memory_snapshot: dict[str, Any],
        public_messages: list[dict[str, Any]],
        observation_message_ids: list[str],
        decision_payload: dict[str, Any] | None = None,
        draft_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current_index = _read_json(self.context_index_path, {"counter": 0})
        bundle_counter = int(current_index.get("counter", 0)) + 1
        bundle_revision = f"ctx-{bundle_counter}"
        bundle_id = f"{self.agent_id}-{bundle_revision}"
        recent_count = int(self.context_config.get("recent_public_message_count", 20))
        selected_messages = public_messages[-recent_count:]
        latest_segment = None
        segments = cp_snapshot.get("segments", [])
        if isinstance(segments, list) and segments:
            latest_segment = segments[-1]
        selected_public_refs = [str(item["message_id"]) for item in selected_messages]
        if latest_segment is not None and latest_segment.get("segment_id"):
            selected_public_refs.append(str(latest_segment["segment_id"]))
        selected_private_refs: list[str] = []
        if (
            self.context_module_id == "context.simple"
            and self.memory_module_id != "memory.no-memory"
        ):
            for node in memory_snapshot["payload"].get("nodes", []):
                selected_private_refs.append(f"node:{node['memory_node_id']}")
            for overlay in memory_snapshot["payload"].get("overlay", []):
                selected_private_refs.append(f"overlay:{overlay['projection_object_ref']}")
        selection_trace = [
            f"phase={phase}",
            f"context_module={self.context_module_id}",
            f"memory_module={self.memory_module_id}",
            f"messages={len(selected_messages)}",
            f"observation_batch={len(observation_message_ids)}",
        ]
        rendered_items = [
            f"[persona] {self.attention_prior}",
            f"[conversation_seq] {conversation_seq}",
        ]
        if latest_segment is not None:
            rendered_items.append(
                "[segment] "
                + json.dumps(
                    {
                        "title": latest_segment.get("title"),
                        "summary": latest_segment.get("summary"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        for message in selected_messages:
            rendered_items.append(
                f"[message:{message['conversation_seq']}] {message['content_markdown']}"
            )
        if (
            self.context_module_id == "context.simple"
            and self.memory_module_id != "memory.no-memory"
        ):
            rendered_items.append(
                "[memory] "
                + json.dumps(memory_snapshot["payload"], ensure_ascii=False, sort_keys=True)
            )
        if decision_payload is not None:
            rendered_items.append(
                "[decision] " + json.dumps(decision_payload, ensure_ascii=False, sort_keys=True)
            )
        if draft_payload is not None:
            rendered_items.append(
                "[draft] " + json.dumps(draft_payload, ensure_ascii=False, sort_keys=True)
            )
        bundle = {
            "bundle_id": bundle_id,
            "revision": bundle_revision,
            "phase": phase,
            "conversation_seq": conversation_seq,
            "projection_revision": projection_revision,
            "memory_revision": memory_snapshot["revision"],
            "messages": deepcopy(selected_messages),
            "selected_public_refs": selected_public_refs,
            "selected_private_refs": selected_private_refs,
            "selection_trace": selection_trace,
            "observation_message_ids": list(observation_message_ids),
            "estimated_tokens": _token_estimate(rendered_items),
            "prompt_versions": {"prompt_version": self.prompt_version},
            "rendered_items": rendered_items,
            "as_of_time": as_of_time,
        }
        _write_json(self.context_dir / f"{bundle_revision}.json", bundle)
        _write_json(self.context_index_path, {"counter": bundle_counter})
        return bundle

    def _apply_operations(
        self,
        payload: dict[str, Any],
        *,
        operations: list[dict[str, Any]],
        run_id: str,
        as_of_time: str,
    ) -> None:
        nodes = payload.setdefault("nodes", [])
        relations = payload.setdefault("relations", [])
        overlay = payload.setdefault("overlay", [])
        applied_operations = payload.setdefault("applied_operations", [])
        for operation in operations:
            op_type = str(operation.get("op", "record"))
            if op_type == "upsert_node":
                node = deepcopy(operation.get("node", {}))
                node_digest = sha1(
                    json.dumps(node, sort_keys=True).encode("utf-8")
                ).hexdigest()[:10]
                node_id = str(node.get("memory_node_id") or f"mem-node-{node_digest}")
                node["memory_node_id"] = node_id
                node.setdefault("created_by_run_id", run_id)
                node.setdefault("created_at", as_of_time)
                node.setdefault("last_touched_at", as_of_time)
                for index, current in enumerate(nodes):
                    if current.get("memory_node_id") == node_id:
                        nodes[index] = node
                        break
                else:
                    nodes.append(node)
            elif op_type == "add_relation":
                relation = deepcopy(operation)
                relation.setdefault("created_by_run_id", run_id)
                relation.setdefault("created_at", as_of_time)
                relations.append(relation)
            elif op_type == "set_overlay":
                update = deepcopy(operation)
                object_ref = str(update.get("projection_object_ref", ""))
                existing = next(
                    (item for item in overlay if item.get("projection_object_ref") == object_ref),
                    None,
                )
                if existing is None:
                    record = {
                        "projection_strategy_id": str(update.get("projection_strategy_id", "")),
                        "projection_revision": int(update.get("projection_revision", 0)),
                        "projection_object_ref": object_ref,
                        "salience": update.get("salience", 0),
                        "stance": update.get("stance"),
                        "familiarity": update.get("familiarity", 0),
                        "commitment": update.get("commitment", 0),
                        "unresolved": bool(update.get("unresolved", False)),
                        "last_touched_at": as_of_time,
                    }
                    overlay.append(record)
                else:
                    existing.update(
                        {
                            key: value
                            for key, value in update.items()
                            if key != "op" and value is not None
                        }
                    )
                    existing["last_touched_at"] = as_of_time
            applied_operations.append(
                {
                    "op": op_type,
                    "run_id": run_id,
                    "applied_at": as_of_time,
                    "payload": deepcopy(operation),
                }
            )
