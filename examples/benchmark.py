"""Reproducible local execution benchmark without a model or external API calls."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import tempfile
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any

from gryphon.config import GryphonConfig
from gryphon.runtime.cache import CacheStore
from gryphon.runtime.executor import CodeExecutor
from gryphon.runtime.registry import Registry
from gryphon.utils.logging import setup_logging


def _arguments() -> argparse.Namespace:
    """Accept bounded workload sizes and explicitly selected isolation profiles."""
    parser = argparse.ArgumentParser(description="Benchmark Gryphon's real local execution pipeline")
    parser.add_argument("--iterations", type=int, default=25)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--profile", choices=("restricted", "docker"), default="restricted")
    parser.add_argument("--docker-runtime", default="runsc")
    args = parser.parse_args()
    if not 1 <= args.iterations <= 1000 or not 1 <= args.concurrency <= 16:
        parser.error("iterations must be 1–1000; concurrency must be 1–16")
    return args


async def _workload(executor: CodeExecutor, iterations: int, concurrency: int) -> dict[str, Any]:
    """Measure validated scalar computations, including durable admission and receipts."""
    semaphore = asyncio.Semaphore(concurrency)
    durations: list[float] = []
    failures: list[str] = []

    async def execute(index: int) -> None:
        """Measure one deterministic invocation without upstream or model calls."""
        async with semaphore:
            started = time.perf_counter()
            result = await executor.execute(
                "result = sum(inputs['values'])",
                "sum numeric values",
                inputs={"values": list(range(1000))},
                idempotency_key=f"benchmark-{index}",
            )
            durations.append((time.perf_counter() - started) * 1000)
            if not result.success or result.data != 499500:
                failures.append(result.error_type or "incorrect_result")

    started = time.perf_counter()
    await asyncio.gather(*(execute(index) for index in range(iterations)))
    elapsed = time.perf_counter() - started
    ordered = sorted(durations)
    return {
        "iterations": iterations,
        "concurrency": concurrency,
        "validated_successes": iterations - len(failures),
        "failures": failures,
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
        "wall_seconds": round(elapsed, 3),
        "executions_per_second": round(iterations / elapsed, 2),
    }


async def benchmark(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    """Create disposable stores and benchmark one explicit execution profile."""
    settings_options: dict[str, Any] = {"_env_file": None}
    config = GryphonConfig(
        **settings_options,
        compiled_output_dir=str(root / "compiled"),
        cache_db_path=str(root / "cache.db"),
        run_db_path=str(root / "runs.db"),
        artifact_dir=str(root / "artifacts"),
        sandbox_mode=args.profile,
        docker_runtime=args.docker_runtime,
        max_concurrent_executions=args.concurrency,
        run_max_entries=args.iterations + 10,
        compile_on_startup=False,
    )
    registry = Registry(config.compiled_output_dir)
    cache = CacheStore(config.cache_db_path)
    await cache.initialize()
    executor = CodeExecutor(config, cache, registry)
    try:
        started = time.perf_counter()
        await executor.startup()
        startup_ms = (time.perf_counter() - started) * 1000
        report = await _workload(executor, args.iterations, args.concurrency)
        report.update(
            profile=args.profile,
            startup_ms=round(startup_ms, 3),
            fastmcp=version("fastmcp"),
            monty=version("pydantic-monty"),
            python=sys.version.split()[0],
        )
        return report
    finally:
        await executor.shutdown()
        await cache.close()


if __name__ == "__main__":
    setup_logging("ERROR")
    arguments = _arguments()
    with tempfile.TemporaryDirectory(prefix="gryphon-benchmark-") as directory:
        output = asyncio.run(benchmark(arguments, Path(directory)))
    sys.stdout.write(json.dumps(output, indent=2) + "\n")
    sys.exit(1 if output["failures"] else 0)
