from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
from pal_chat_server.worker_main import RawWebSocketClient

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_JSON = ROOT_DIR / "var" / "metrics" / "stage7-performance-baseline.json"
DEFAULT_REPORT_MD = ROOT_DIR / "docs" / "stage7-performance-baseline.md"


@dataclass
class Sample:
    name: str
    threshold_ms: float
    durations_ms: list[float]
    note: str
    raw_samples: list[dict[str, Any]] | None = None

    @property
    def avg_ms(self) -> float:
        return statistics.mean(self.durations_ms)

    @property
    def p95_ms(self) -> float:
        if len(self.durations_ms) == 1:
            return self.durations_ms[0]
        return statistics.quantiles(self.durations_ms, n=100)[94]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "threshold_ms": self.threshold_ms,
            "avg_ms": round(self.avg_ms, 2),
            "p95_ms": round(self.p95_ms, 2),
            "runs": len(self.durations_ms),
            "note": self.note,
            "durations_ms": [round(value, 2) for value in self.durations_ms],
            "raw_samples": self.raw_samples or [],
        }


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 30.0,
    interval: float = 0.1,
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise TimeoutError("Timed out waiting for background processing.")


def wait_for_idle_barrier(client: httpx.Client, conversation_id: str) -> dict[str, Any]:
    last_payload: dict[str, Any] | None = None

    def is_idle() -> bool:
        nonlocal last_payload
        response = client.get(f"/api/v1/conversations/{conversation_id}/idle-barrier")
        response.raise_for_status()
        last_payload = cast(dict[str, Any], response.json())
        return bool(last_payload["idle"])

    wait_until(is_idle, timeout=30.0, interval=0.2)
    assert last_payload is not None
    return last_payload


def fetch_trace(client: httpx.Client, trace_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/perf-traces/{trace_id}")
    response.raise_for_status()
    return cast(dict[str, Any], response.json())


def measure_get(
    client: httpx.Client,
    path: str,
    runs: int,
    *,
    cold_label: str = "cold",
    warm_label: str = "warm",
) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for index in range(runs):
        started = time.perf_counter()
        response = client.get(path, headers={"X-PAL-Perf-Trace": "1"})
        response.raise_for_status()
        trace_id = response.headers.get("X-PAL-Perf-Trace")
        samples.append(
            {
                "run": index + 1,
                "kind": cold_label if index == 0 else warm_label,
                "duration_ms": (time.perf_counter() - started) * 1000,
                "path": path,
                "trace_id": trace_id,
                "trace": fetch_trace(client, trace_id) if trace_id else None,
            }
        )
    return samples


def sample_durations(raw_samples: list[dict[str, Any]], field: str = "duration_ms") -> list[float]:
    return [float(sample[field]) for sample in raw_samples]


def clone_default_profile(client: httpx.Client) -> dict[str, Any]:
    response = client.post("/api/v1/profile-templates/default-natural-chat-v1/clone")
    response.raise_for_status()
    return dict(response.json()["profile"])


def create_conversation(
    client: httpx.Client,
    *,
    title: str,
    draft_profile: dict[str, Any],
) -> str:
    response = client.post(
        "/api/v1/conversations",
        json={"title": title, "draft_profile": draft_profile},
    )
    response.raise_for_status()
    conversation_id = str(response.json()["id"])
    validate = client.post(f"/api/v1/conversations/{conversation_id}/validate")
    validate.raise_for_status()
    started = client.post(f"/api/v1/conversations/{conversation_id}/start")
    started.raise_for_status()
    return conversation_id


def create_history_conversation(
    client: httpx.Client,
    *,
    title: str,
    draft_profile: dict[str, Any],
) -> str:
    conversation_id = create_conversation(client, title=title, draft_profile=draft_profile)
    end_conversation(client, conversation_id)
    return conversation_id


def end_conversation(client: httpx.Client, conversation_id: str) -> None:
    response = client.post(f"/api/v1/conversations/{conversation_id}/end")
    response.raise_for_status()


def end_active_conversations(client: httpx.Client) -> None:
    response = client.get("/api/v1/conversations")
    response.raise_for_status()
    for item in response.json()["items"]:
        if item["status"] in {"running", "paused"}:
            end_conversation(client, str(item["id"]))


def submit_message(
    client: httpx.Client,
    conversation_id: str,
    *,
    client_message_id: str,
    content_markdown: str,
    mentions: list[str] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=headers,
        json={
            "client_message_id": client_message_id,
            "content_markdown": content_markdown,
            "mentions": mentions or [],
            "responds_to": [],
            "primary_reply_to": None,
        },
    )


def message_count(client: httpx.Client, conversation_id: str) -> int:
    detail = client.get(f"/api/v1/conversations/{conversation_id}")
    detail.raise_for_status()
    authority = detail.json().get("runtime_authority") or {}
    return int(authority.get("latest_reliable_seq", 0))


def no_running_runs(client: httpx.Client, conversation_id: str) -> bool:
    response = client.get(f"/api/v1/conversations/{conversation_id}/runs")
    response.raise_for_status()
    runs = response.json()["items"]
    return all(item["status"] != "RUNNING" for item in runs)

def scripted_profile_from_clone(cloned: dict[str, Any]) -> dict[str, Any]:
    profile = json.loads(json.dumps(cloned))
    modules = dict(profile["modules"])
    modules["model_adapter"] = {
        "module_id": "model.scripted",
        "config": {
            "script": [
                {
                    "purpose": "decision",
                    "agent_id": "agent-a",
                    "match_contains": "@A perf seed",
                    "output_json": {"should_reply": True, "reply_key": "perf-seed"},
                },
                {
                    "purpose": "action",
                    "agent_id": "agent-a",
                    "match_contains": '"reply_key": "perf-seed"',
                    "output_json": {
                        "content_markdown": "A perf seed acknowledged",
                        "mentions": [],
                        "primary_reply_to": None,
                        "responds_to": [],
                    },
                },
            ]
        },
    }
    modules["memory"] = {"module_id": "memory.graph-overlay", "config": {}}
    modules["context_assembly"] = {"module_id": "context.simple", "config": {}}
    profile["modules"] = modules
    metadata = dict(profile.get("metadata") or {})
    metadata["phase"] = 4
    metadata["agent_runtime_enabled"] = True
    profile["metadata"] = metadata
    return profile


def prepare_representative_data(
    client: httpx.Client,
    *,
    total_messages: int,
    measured_posts: int,
) -> str:
    end_active_conversations(client)
    seed_suffix = int(time.time())
    cloned = clone_default_profile(client)
    primary_profile = scripted_profile_from_clone(cloned)
    primary_title = f"Stage7 Perf Primary {seed_suffix}"
    conversation_id = create_conversation(
        client,
        title=primary_title,
        draft_profile=primary_profile,
    )

    expected_messages = 0
    for index in range(5):
        response = submit_message(
            client,
            conversation_id,
            client_message_id=f"perf-seed-{index}",
            content_markdown=f"@A perf seed {index}",
            mentions=["agent-a"],
        )
        response.raise_for_status()
        expected_messages += 2
        expected_total = expected_messages
        wait_until(
            lambda expected_total=expected_total: message_count(client, conversation_id)
            >= expected_total
            and no_running_runs(client, conversation_id),
            timeout=30.0,
        )

    filler_target = max(total_messages - measured_posts, expected_messages)
    current_count = message_count(client, conversation_id)
    filler_index = 0
    while current_count < filler_target:
        response = submit_message(
            client,
            conversation_id,
            client_message_id=f"perf-fill-{filler_index}",
            content_markdown=f"plain perf filler {filler_index}",
        )
        response.raise_for_status()
        filler_index += 1
        current_count += 1
    return conversation_id


def measure_post_commits(
    client: httpx.Client,
    conversation_id: str,
    *,
    runs: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    post_samples: list[dict[str, Any]] = []
    ws_visible_samples: list[dict[str, Any]] = []
    ws_url = str(client.base_url).replace("http://", "ws://").rstrip("/")
    ws = RawWebSocketClient(
        f"{ws_url}/ws/v1/conversations/{conversation_id}",
        {},
    )
    ws.connect()
    assert ws.sock is not None
    ws.sock.settimeout(5)
    for index in range(runs):
        if index == 0:
            snapshot = ws.recv_json()
            if snapshot.get("event_type") != "session.snapshot":
                raise RuntimeError("expected websocket session snapshot before perf sampling")
        client_message_id = f"perf-measure-{index}"
        started = time.perf_counter()
        response = submit_message(
            client,
            conversation_id,
            client_message_id=client_message_id,
            content_markdown=f"measured perf commit {index}",
            headers={"X-PAL-Perf-Trace": "1"},
        )
        response.raise_for_status()
        post_ms = (time.perf_counter() - started) * 1000
        trace_id = response.headers.get("X-PAL-Perf-Trace")
        post_samples.append(
            {
                "run": index + 1,
                "kind": "cold" if index == 0 else "warm",
                "duration_ms": post_ms,
                "client_message_id": client_message_id,
                "trace_id": trace_id,
            }
        )

        visible_started = time.perf_counter()
        while True:
            event = ws.recv_json()
            if event.get("event_type") != "message.committed":
                continue
            payload = event.get("payload") or {}
            message = payload.get("message") or {}
            if message.get("client_message_id") != client_message_id:
                continue
            break
        ws_visible_samples.append(
            {
                "run": index + 1,
                "kind": "cold" if index == 0 else "warm",
                "duration_ms": (time.perf_counter() - visible_started) * 1000,
                "client_message_id": client_message_id,
            }
        )
        if trace_id is not None:
            wait_until(
                lambda trace_id=trace_id: any(
                    item["name"] == "dispatcher.completed"
                    for item in fetch_trace(client, trace_id).get("dispatcher_segments", [])
                ),
                timeout=10.0,
                interval=0.1,
            )
            trace_payload = fetch_trace(client, trace_id)
            post_samples[-1]["trace"] = trace_payload
            ws_visible_samples[-1]["trace"] = trace_payload
    ws.close()
    return post_samples, ws_visible_samples


def measure_ui_sidebar(
    *,
    web_base_url: str,
    primary_title: str,
    secondary_title: str,
    runs: int,
) -> Sample:
    command = [
        "node",
        "apps/web/scripts/perf-baseline.mjs",
        "--base-url",
        web_base_url,
        "--primary-title",
        primary_title,
        "--secondary-title",
        secondary_title,
        "--runs",
        str(runs),
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    return Sample(
        "ui_sidebar_select_1440x900",
        threshold_ms=float(payload["threshold_ms"]),
        durations_ms=[float(value) for value in payload["durations_ms"]],
        note="Playwright headless; selects representative primary conversation from sidebar.",
        raw_samples=cast(list[dict[str, Any]], payload.get("raw_samples") or []),
    )


def render_markdown(
    *,
    generated_at: str,
    conversation_id: str,
    samples: list[Sample],
    auxiliary: list[Sample],
) -> str:
    lines = [
        "# Stage 7 Performance Baseline",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Representative conversation: `{conversation_id}`",
        "- Dataset shape: 5 scripted agent replies + 1000 public messages path",
        "",
        "## Exit Thresholds",
        "",
        "| Metric | Threshold | Avg (ms) | P95 (ms) | Runs | Result |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for sample in samples:
        result = "PASS" if sample.p95_ms <= sample.threshold_ms else "FAIL"
        lines.append(
            f"| `{sample.name}` | <= {sample.threshold_ms:.0f} | "
            f"{sample.avg_ms:.2f} | {sample.p95_ms:.2f} | {len(sample.durations_ms)} | {result} |"
        )
    lines.extend(
        [
            "",
            "## Auxiliary Samples",
            "",
            "| Metric | Avg (ms) | P95 (ms) | Runs | Note |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for sample in auxiliary:
        lines.append(
            f"| `{sample.name}` | {sample.avg_ms:.2f} | {sample.p95_ms:.2f} | "
            f"{len(sample.durations_ms)} | {sample.note} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--web-base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--ui-runs", type=int, default=10)
    parser.add_argument("--total-messages", type=int, default=1000)
    parser.add_argument("--report-json", type=Path, default=DEFAULT_REPORT_JSON)
    parser.add_argument("--report-md", type=Path, default=DEFAULT_REPORT_MD)
    args = parser.parse_args()

    generated_at = datetime.now(UTC).isoformat()
    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_md.parent.mkdir(parents=True, exist_ok=True)

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        conversation_id = prepare_representative_data(
            client,
            total_messages=args.total_messages,
            measured_posts=args.runs,
        )
        idle_before_sampling = wait_for_idle_barrier(client, conversation_id)
        primary_detail = client.get(f"/api/v1/conversations/{conversation_id}")
        primary_detail.raise_for_status()
        primary_title = primary_detail.json()["conversation"]["title"]

        post_commit_samples, ws_visible_samples = measure_post_commits(
            client,
            conversation_id,
            runs=args.runs,
        )
        post_commit = Sample(
            "message_commit_non_model",
            threshold_ms=50,
            durations_ms=sample_durations(post_commit_samples),
            note="User message commit without mentions; no provider call should occur.",
            raw_samples=post_commit_samples,
        )
        ws_visible = Sample(
            "message_commit_ws_visible",
            threshold_ms=100,
            durations_ms=sample_durations(ws_visible_samples),
            note=(
                "Elapsed time from POST return to matching message.committed "
                "becoming visible on WS."
            ),
            raw_samples=ws_visible_samples,
        )
        recent_message_samples = measure_get(
            client,
            f"/api/v1/conversations/{conversation_id}/messages?limit=1000",
            args.runs,
        )
        recent_messages = Sample(
            "messages_recent_1000",
            threshold_ms=200,
            durations_ms=sample_durations(recent_message_samples),
            note="Reads the latest 1000 public messages from the representative conversation.",
            raw_samples=recent_message_samples,
        )
        cost_breakdown_samples = measure_get(
            client,
            f"/api/v1/conversations/{conversation_id}/metrics/cost-breakdown",
            args.runs,
        )
        cost_breakdown = Sample(
            "cost_breakdown",
            threshold_ms=200,
            durations_ms=sample_durations(cost_breakdown_samples),
            note="Auxiliary monitoring endpoint on the same representative conversation.",
            raw_samples=cost_breakdown_samples,
        )
        ready_health_samples = measure_get(client, "/health/ready", args.runs)
        ready_health = Sample(
            "ready_health",
            threshold_ms=200,
            durations_ms=sample_durations(ready_health_samples),
            note="Service health baseline under the seeded dataset.",
            raw_samples=ready_health_samples,
        )
        end_conversation(client, conversation_id)
        secondary_title = f"Stage7 Perf Secondary {int(time.time())}"
        create_history_conversation(
            client,
            title=secondary_title,
            draft_profile=scripted_profile_from_clone(clone_default_profile(client)),
        )

    ui_sidebar = measure_ui_sidebar(
        web_base_url=args.web_base_url,
        primary_title=primary_title,
        secondary_title=secondary_title,
        runs=args.ui_runs,
    )
    ui_threshold_sample = Sample(
        "ui_sidebar_select_1440x900",
        threshold_ms=100,
        durations_ms=ui_sidebar.durations_ms,
        note=ui_sidebar.note,
    )

    exit_samples = [ui_threshold_sample, post_commit, recent_messages]
    auxiliary = [ready_health, cost_breakdown, ws_visible]
    report_payload = {
        "generated_at": generated_at,
        "conversation_id": conversation_id,
        "idle_barrier_before_sampling": idle_before_sampling,
        "exit_samples": [sample.to_dict() for sample in exit_samples],
        "auxiliary_samples": [sample.to_dict() for sample in auxiliary],
    }
    args.report_json.write_text(
        f"{json.dumps(report_payload, ensure_ascii=False, indent=2)}\n",
        encoding="utf-8",
    )
    args.report_md.write_text(
        render_markdown(
            generated_at=generated_at,
            conversation_id=conversation_id,
            samples=exit_samples,
            auxiliary=auxiliary,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "report_json": str(args.report_json),
                "report_md": str(args.report_md),
                "conversation_id": conversation_id,
                "samples": report_payload["exit_samples"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
