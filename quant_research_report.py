"""P1 CLI: read committed P0 SQLite research and emit an offline report."""
from research.safe_paths import unsafe_outputs
import argparse
import json
import sqlite3
from pathlib import Path
from research.statistical_analysis import analyze, load_rows, load_benchmark, render_markdown


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='trading_engine.db')
    parser.add_argument('--setup')
    parser.add_argument('--horizon', type=int, choices=(5, 10, 30))
    parser.add_argument('--rule-version')
    parser.add_argument('--holdout-start', help='ISO date, fixed before inspecting holdout')
    parser.add_argument('--benchmark-csv')
    parser.add_argument('--benchmark-name')
    parser.add_argument('--seed', type=int, default=1729)
    parser.add_argument('--resamples', type=int, default=2000)
    parser.add_argument('--min-samples', type=int, default=30)
    parser.add_argument('--min-days', type=int, default=20)
    parser.add_argument('--output', help='Optional Markdown file; default prints to stdout')
    parser.add_argument('--json-output', help='Optional machine-readable report file')
    args = parser.parse_args(argv)
    if bool(args.benchmark_csv) != bool(args.benchmark_name):
        parser.error('--benchmark-csv and --benchmark-name must be supplied together')
    sources = {Path(path).resolve() for path in (args.db, args.benchmark_csv) if path}
    outputs = [Path(path).resolve() for path in (args.output, args.json_output) if path]
    if unsafe_outputs(sources, outputs):
        parser.error('Report output paths must differ from inputs and each other')
    try:
        benchmark = load_benchmark(args.benchmark_csv, args.benchmark_name) if args.benchmark_csv else None
        report = analyze(load_rows(args.db), benchmark=benchmark, benchmark_name=args.benchmark_name,
            holdout_start=args.holdout_start, setup=args.setup, horizon=args.horizon,
            rule_version=args.rule_version, seed=args.seed, resamples=args.resamples,
            min_samples=args.min_samples, min_days=args.min_days)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(2, f'Research report failed: {error}\n')
    markdown = render_markdown(report)
    if args.output:
        Path(args.output).write_text(markdown, encoding='utf-8')
    else:
        print(markdown, end='')
    if args.json_output:
        Path(args.json_output).write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    main()
