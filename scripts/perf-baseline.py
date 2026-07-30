from __future__ import annotations

import argparse
import statistics
import time
from dataclasses import dataclass

import httpx


@dataclass
class Sample:
    name: str
    durations_ms: list[float]

    @property
    def p95_ms(self) -> float:
        if len(self.durations_ms) == 1:
            return self.durations_ms[0]
        return statistics.quantiles(self.durations_ms, n=100)[94]


def measure(client: httpx.Client, path: str, runs: int) -> list[float]:
    durations: list[float] = []
    for _ in range(runs):
        started = time.perf_counter()
        response = client.get(path)
        response.raise_for_status()
        durations.append((time.perf_counter() - started) * 1000)
    return durations


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--runs", type=int, default=20)
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=10) as client:
        samples = [
            Sample("ready_health", measure(client, "/health/ready", args.runs)),
            Sample(
                "messages_recent_1000",
                measure(
                    client,
                    f"/api/v1/conversations/{args.conversation_id}/messages?limit=1000",
                    args.runs,
                ),
            ),
            Sample(
                "cost_breakdown",
                measure(
                    client,
                    f"/api/v1/conversations/{args.conversation_id}/metrics/cost-breakdown",
                    args.runs,
                ),
            ),
        ]

    for sample in samples:
        print(
            f"{sample.name}: runs={len(sample.durations_ms)} "
            f"avg_ms={statistics.mean(sample.durations_ms):.2f} "
            f"p95_ms={sample.p95_ms:.2f}"
        )


if __name__ == "__main__":
    main()
