"""Prepare reproducible ADR-0023 replay inputs; never changes cluster state."""
import argparse
import hashlib
import json
import random
from pathlib import Path

from workloads import source_text

ARMS = ("local-lb", "local-aware", "pool-lb", "pool-aware")


def sessions(*, seed=23, count=128, turns=4, repeat_ratio=0.9, context_chars=16000):
    if not 0 <= repeat_ratio <= 1 or min(count, turns, context_chars) < 1:
        raise ValueError("invalid workload bounds")
    rng = random.Random(seed)
    for index in range(count):
        shared = rng.random() < repeat_ratio
        key = f"{seed}:shared" if shared else f"{seed}:unique:{index}"
        messages = [{"role": "system", "content": f"Workspace {key}\n" + source_text(key, context_chars)}]
        requests = []
        for turn in range(turns):
            messages.append({"role": "user", "content": f"Session {index}, step {turn}: explain the concurrency bug."})
            requests.append({"messages": list(messages), "temperature": 0, "max_tokens": 128})
            # Fixed history keeps inputs identical across all four experiment arms.
            messages.append({"role": "assistant", "content": "Inspect isolation, invalidation and the retry boundary."})
        yield {"requests": requests}


def prepare(directory: Path, *, seed=23, count=128):
    directory.mkdir(parents=True, exist_ok=False)
    workloads = {}
    for name, ratio, turns in (("high-repeat", .9, 1), ("low-repeat", .05, 1), ("multi-turn", .9, 4)):
        payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in sessions(
            seed=seed, count=count, repeat_ratio=ratio, turns=turns))
        (directory / f"{name}.jsonl").write_text(payload, encoding="utf-8")
        workloads[name] = {"sha256": hashlib.sha256(payload.encode()).hexdigest(), "sessions": count, "turns": turns,
                           "shared_prefix_probability": ratio}
    rng = random.Random(seed)
    order = []
    for repeat in range(3):
        arms = list(ARMS)
        rng.shuffle(arms)
        order.extend({"repeat": repeat + 1, "arm": arm} for arm in arms)
    manifest = {"schema_version": 1, "seed": seed, "workloads": workloads, "run_order": order,
                "session_affinity": False, "runtime_certified": False,
                "required_provenance": ["model_digest", "runtime_image_digest", "token_profile", "motor_commit",
                    "memcache_image_digest", "cann", "driver", "hardware", "policy", "profile", "pool_revision",
                    "concurrency", "cold_warm_procedure", "router_replicas", "source_commits"],
                "lifecycle": ["2P2D cross-pair", "add P3/D3", "remove P1/D1 after draining requests",
                    "controller takeover", "conductor restart", "MetaService restart", "storage provider loss"],
                "thresholds": {"high_repeat_ttft_p95_improvement": .10, "low_repeat_max_regression": .05,
                    "normal_query_p95_ms": 20, "unavailable_query_p99_ms": 55}}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def compare(baseline, candidate, *, high_repeat):
    """One pair only; acceptance still requires three runs and correctness evidence."""
    b = baseline.get("ttft_ms", {}).get("p95")
    c = candidate.get("ttft_ms", {}).get("p95")
    bt, ct = baseline.get("successful_requests_per_s"), candidate.get("successful_requests_per_s")
    if not b or c is None or not bt or ct is None or min(baseline.get("success", 0), candidate.get("success", 0)) < 100:
        return {"status": "insufficient_evidence"}
    latency_ok = c <= b * (.9 if high_repeat else 1.05)
    throughput_ok = high_repeat or ct >= bt * .95
    def error_rate(value):
        return value.get("failed", 0) / max(1, value.get("requests", 0))
    return {"status": "pair_pass" if latency_ok and throughput_ok and error_rate(candidate) <= error_rate(baseline) else "pair_fail",
            "ttft_p95_change": c / b - 1, "throughput_change": ct / bt - 1,
            "hardware_acceptance": "pending correctness, three repeats, query latency and lifecycle evidence"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--seed", type=int, default=23)
    parser.add_argument("--sessions", type=int, default=128)
    args = parser.parse_args()
    prepare(args.directory, seed=args.seed, count=args.sessions)
