#!/usr/bin/env python3
"""
Read-only import audit for the ashakil commercial client.

This does not import application code. It parses Python AST imports so it can
run even when the app is currently broken.

Usage from the VPS app root:
  ./venv/bin/python scripts/audit_live_imports.py
  ./venv/bin/python scripts/audit_live_imports.py --show-reachable
  ./venv/bin/python scripts/audit_live_imports.py --show-candidates
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict, deque
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]

ENTRY_FILES = [
    APP_ROOT / "main.py",
    APP_ROOT / "celery_worker.py",
    APP_ROOT / "celery_app.py",
]

KEEP_HINTS = {
    "main.py",
    "celery_app.py",
    "celery_worker.py",
    "config.py",
    "dependencies.py",
    "database/base.py",
    "database/connection.py",
    "routes/auth.py",
    "routes/pages.py",
    "routes/paper_trade_bot.py",
    "routes/log_analysis.py",
    "routes/schwab_trade.py",
    "routes/schwab/schwab_api.py",
    "routes/schwab/schwab_auth.py",
    "routes/schwab/schwab_history.py",
    "modules/users/routes.py",
    "modules/users/account_routes.py",
    "modules/dashboard/routes.py",
    "modules/broker/routes.py",
    "modules/replay/routes.py",
    "tasks/stock_tasks.py",
    "tasks/schwab_tasks.py",
    "tasks/replay_tasks.py",
    "trading/runners/stock_bot_runner.py",
    "scripts/replay/data_ingest.py",
    "scripts/replay/orchestrator.py",
    "scripts/replay/replay_data_provider.py",
    "scripts/stock_algos/Algo1_MM.py",
    "scripts/stock_algos/Algo2_MM.py",
    "scripts/stock_algos/Algo3_MM.py",
    "scripts/stock_algos/Algo4_MM.py",
    "scripts/stock_algos/algoMM_replay_runner.py",
    "scripts/stock_algos/algoMM_runner.py",
    "scripts/stock_algos/algo_runner.py",
    "scripts/research/Featureset_1.py",
    "scripts/research/Featureset_2.py",
    "scripts/research/Featureset_3.py",
    "scripts/ml/mm_live_helpers.py",
    "services/email_service.py",
    "services/log_analysis_service.py",
    "services/paper_trade_service.py",
    "services/replay_process.py",
    "services/replay_trade_service.py",
    "services/trade_service.py",
}

BAD_KEYWORDS = [
    "option",
    "spx",
    "zero_dte",
    "zero-dte",
    "notification",
    "predict_price",
    "ai_evaluate",
    "backtest",
    "db_admin",
    "forum",
]


def rel(path: Path) -> str:
    return path.relative_to(APP_ROOT).as_posix()


def module_name_for(path: Path) -> str | None:
    if not path.exists() or path.suffix != ".py":
        return None
    r = rel(path)
    if r == "main.py":
        return "app.main"
    if "/" not in r:
        return f"app.{path.stem}"
    return "app." + r[:-3].replace("/", ".")


def path_for_module(module: str) -> Path | None:
    if not module.startswith("app."):
        return None

    parts = module.split(".")[1:]
    if not parts:
        return None

    py_file = APP_ROOT.joinpath(*parts).with_suffix(".py")
    if py_file.exists():
        return py_file

    init_file = APP_ROOT.joinpath(*parts, "__init__.py")
    if init_file.exists():
        return init_file

    return py_file


def internal_imports(path: Path) -> tuple[set[str], list[str]]:
    imports: set[str] = set()
    errors: list[str] = []

    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except Exception as exc:
        return imports, [f"PARSE_ERROR: {exc}"]

    current_module = module_name_for(path) or ""
    current_pkg = current_module.rsplit(".", 1)[0] if "." in current_module else current_module

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name == "app" or name.startswith("app."):
                    imports.add(name)

        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""

            if node.level:
                pkg_parts = current_pkg.split(".")
                if node.level > len(pkg_parts):
                    continue
                base = ".".join(pkg_parts[: len(pkg_parts) - node.level + 1])
                mod = f"{base}.{mod}" if mod else base

            if mod == "app" or mod.startswith("app."):
                imports.add(mod)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    imports.add(f"{mod}.{alias.name}")

    return imports, errors


def build_graph() -> tuple[dict[str, set[str]], dict[str, list[str]], dict[str, Path]]:
    module_to_path: dict[str, Path] = {}
    for path in APP_ROOT.rglob("*.py"):
        if any(part.startswith("_cleanup_archive_") for part in path.parts):
            continue
        if "__pycache__" in path.parts:
            continue
        mod = module_name_for(path)
        if mod:
            module_to_path[mod] = path

    graph: dict[str, set[str]] = defaultdict(set)
    parse_errors: dict[str, list[str]] = {}

    for mod, path in module_to_path.items():
        imports, errors = internal_imports(path)
        if errors:
            parse_errors[mod] = errors
        for imported in imports:
            target = resolve_import(imported, module_to_path)
            if target:
                graph[mod].add(target)
            elif imported.startswith("app."):
                graph[mod].add(f"MISSING:{imported}")

    return graph, parse_errors, module_to_path


def resolve_import(imported: str, module_to_path: dict[str, Path]) -> str | None:
    probe = imported
    while probe.startswith("app."):
        if probe in module_to_path:
            return probe
        if "." not in probe:
            break
        probe = probe.rsplit(".", 1)[0]
    return None


def reachable_from_entries(graph: dict[str, set[str]]) -> set[str]:
    starts = []
    for path in ENTRY_FILES:
        mod = module_name_for(path)
        if mod:
            starts.append(mod)

    seen: set[str] = set()
    q = deque(starts)
    while q:
        mod = q.popleft()
        if mod in seen:
            continue
        seen.add(mod)
        for nxt in sorted(graph.get(mod, ())):
            if nxt.startswith("MISSING:"):
                seen.add(nxt)
                continue
            if nxt not in seen:
                q.append(nxt)
    return seen


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--show-reachable", action="store_true")
    parser.add_argument("--show-candidates", action="store_true")
    args = parser.parse_args()

    graph, parse_errors, module_to_path = build_graph()
    reachable = reachable_from_entries(graph)

    missing = sorted(m for m in reachable if m.startswith("MISSING:"))
    reachable_files = sorted(
        rel(module_to_path[m])
        for m in reachable
        if not m.startswith("MISSING:") and m in module_to_path
    )

    all_files = sorted(rel(p) for p in module_to_path.values())
    candidate_files = [
        f for f in all_files
        if f not in reachable_files
        and f not in KEEP_HINTS
        and not f.endswith("__init__.py")
    ]

    suspicious_reachable = [
        f for f in reachable_files
        if any(k in f.lower() for k in BAD_KEYWORDS)
    ]

    print(f"app root: {APP_ROOT}")
    print(f"python files: {len(all_files)}")
    print(f"reachable python files from main/celery: {len(reachable_files)}")
    print(f"archive candidates not reachable: {len(candidate_files)}")

    if missing:
        print("\nMISSING INTERNAL IMPORTS:")
        for item in missing:
            print(f"  {item.removeprefix('MISSING:')}")

    if parse_errors:
        print("\nPARSE ERRORS:")
        for mod, errors in sorted(parse_errors.items()):
            path = module_to_path.get(mod)
            label = rel(path) if path else mod
            for err in errors:
                print(f"  {label}: {err}")

    if suspicious_reachable:
        print("\nSUSPICIOUS BUT REACHABLE FILES:")
        for f in suspicious_reachable:
            print(f"  {f}")

    if args.show_reachable:
        print("\nREACHABLE FILES:")
        for f in reachable_files:
            print(f"  {f}")

    if args.show_candidates:
        print("\nARCHIVE CANDIDATES:")
        for f in candidate_files:
            print(f"  {f}")

    print("\nNotes:")
    print("  - If a file is reachable, do not delete it blindly.")
    print("  - app/scripts/replay, app/scripts/stock_algos, app/scripts/research, and app/scripts/ml are runtime code for Replay/Algo1/2/3_MM.")
    print("  - Templates are not fully covered by Python import reachability; check TemplateResponse names too.")

    return 1 if missing or parse_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
