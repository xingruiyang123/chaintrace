"""Generate database-contract-like JSON artifacts for the synthetic A01-A12 smoke cases."""
import argparse
import json
from pathlib import Path
from chaintrace_matcher import Engine, Config, load_fixture


def main():
    p = argparse.ArgumentParser(description="ChainTrace offline synthetic experiment")
    p.add_argument("--input", default="fixtures/synthetic_cases.json")
    p.add_argument("--output", default="outputs")
    args = p.parse_args()
    engine = Engine(load_fixture(args.input), Config())
    bundle = engine.run_bundle()
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    for key in ["analysis_runs", "candidate_matches", "metrics"]:
        (output_dir / (key + ".json")).write_text(
            json.dumps(bundle[key], ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(json.dumps(bundle["metrics"], ensure_ascii=False, indent=2))
    print(f"Generated {len(bundle['candidate_matches'])} candidate/diagnostic records, "
          f"{len(bundle['analysis_runs'])} case-scoped analysis runs in {output_dir}/")


if __name__ == "__main__":
    main()
