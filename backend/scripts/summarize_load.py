import csv
import sys
from pathlib import Path

LABELS = {
    "miss_1x": ("cache miss (retrieve + generate + write)", 1),
    "miss_3x": ("cache miss (retrieve + generate + write)", 3),
    "hit_1x": ("cache hit (Redis)", 1),
    "hit_3x": ("cache hit (Redis)", 3),
    "slowllm_1x": ("cache miss, 400 ms simulated LLM, 150 users", 1),
    "slowllm_3x": ("cache miss, 400 ms simulated LLM, 150 users", 3),
}


def main(out: Path) -> None:
    lines = [
        "| Scenario | API instances | Requests | Failures | Throughput (req/s) | p50 ms | p95 ms | p99 ms | Split across instances |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name, (label, n) in LABELS.items():
        f = out / f"{name}_stats.csv"
        if not f.exists():
            continue
        rows = list(csv.DictReader(f.open()))
        chat = next(r for r in rows if r["Name"].startswith("/api/chat"))
        inst = (out / f"{name}.instances").read_text().strip().replace("requests per instance: ", "") if (out / f"{name}.instances").exists() else ""
        lines.append(
            f"| {label} | {n} | {chat['Request Count']} | {chat['Failure Count']} | {float(chat['Requests/s']):.1f} | {chat['50%']} | {chat['95%']} | {chat['99%']} | {inst} |"
        )
    md = "\n".join(lines) + "\n"
    (out / "summary.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
