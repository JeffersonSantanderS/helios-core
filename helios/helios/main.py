"""Helios v6 — CLI entry point.

Commands: tick, brain, daily, daemon, shadow, status, version,
          list-modules, new-module, config-check
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

# Config loader is side-effect-free on import.
# Heavy imports (engine, scheduler, state) are lazy-imported inside
# command handlers that need them. This prevents side effects like
# insight_engine creating directories on import.
from . import config_loader

log = logging.getLogger("helios.main")


def _helios_base() -> str:
    """Resolve HELIOS_BASE the same way HeliosDB does."""
    return os.environ.get(
        "HELIOS_BASE",
        os.path.join(os.path.expanduser("~"), ".hermes", "helios"),
    )


def setup_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )


def cmd_tick(args):
    from . import engine
    cfg = config_loader.ConfigLoader.load()
    eng = engine.HeliosEngine(cfg=cfg)
    result = eng.tick()
    eng.close()
    print(json.dumps(result, indent=2, default=str))


def cmd_brain(args):
    from . import engine
    cfg = config_loader.ConfigLoader.load()
    eng = engine.HeliosEngine(cfg=cfg)
    result = eng.run_brain()
    eng.close()
    print(json.dumps(result, indent=2, default=str))


def cmd_daily(args):
    from . import engine
    cfg = config_loader.ConfigLoader.load()
    eng = engine.HeliosEngine(cfg=cfg)
    result = eng.run_daily()
    eng.close()
    print(json.dumps(result, indent=2, default=str))


def cmd_shadow(args):
    from . import engine
    cfg = config_loader.ConfigLoader.load()
    eng = engine.HeliosEngine(cfg=cfg)
    result = eng.run_shadow()
    eng.close()
    print(json.dumps(result, indent=2, default=str))


def cmd_daemon(args):
    from . import engine, scheduler
    cfg = config_loader.ConfigLoader.load()
    interval = cfg.get("scheduler", "tick_interval", default=300)
    if interval is None:
        interval = 300
    sch = scheduler.Scheduler(cfg)
    eng = engine.HeliosEngine(cfg=cfg, start_services=True)
    log.info("Helios v6 daemon starting — tick every %ds", interval)
    last_brain: Optional[float] = None
    try:
        while True:
            state_val = {"last_brain": last_brain}
            tasks = sch.what_to_run(state_val) if hasattr(sch, "what_to_run") else ["tick"]
            eng.tick()
            if "brain" in tasks:
                eng.run_brain()
                last_brain = time.time()
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("Daemon shutting down")
    finally:
        eng.close()


def cmd_status(args):
    from . import engine
    cfg = config_loader.ConfigLoader.load()
    eng = engine.HeliosEngine(cfg=cfg)
    result = eng.tick()
    eng.close()
    print(json.dumps(result, indent=2, default=str))


def cmd_priority_latest(args):
    """Show latest priority tick summary from DB or file."""
    from . import engine
    cfg = config_loader.ConfigLoader.load()
    eng = engine.HeliosEngine(cfg=cfg)
    summary = eng.summarizer.generate(hours=1)
    eng.close()
    # Compact display
    print("--- Latest Priority Tick ---")
    t = summary["totals"]
    print(f"Generated: {t['generated']} | Scored: {t['scored']} | "
          f"Selected: {t['selected']} | Suppressed: {t['suppressed']}")
    if summary["top_candidates"]:
        print("\nTop Candidates:")
        for item in summary["top_candidates"][:5]:
            print(f"  • {item['title']} — score: {item['score']:.2f} "
                  f"({item['decision']}) | {item['explanation'][:80]}")
    print(f"\nGenerated at: {summary['generated_at'][:19]}")


def cmd_priority_recent(args):
    """Show priority summary for last N hours."""
    from . import engine
    hours = getattr(args, "hours", 24)
    cfg = config_loader.ConfigLoader.load()
    eng = engine.HeliosEngine(cfg=cfg)
    summary = eng.summarizer.generate(hours=hours)
    eng.close()
    # Full JSON output for piping
    print(json.dumps(summary, indent=2, default=str))


def cmd_self_improvement_status(args):
    """Show self-improvement loop status."""
    from .self_improvement import SelfImprovementIntegration
    si = SelfImprovementIntegration()
    status = si.get_status()
    print("--- Self-Improvement Status ---")
    print(f"Mode:              {status['mode']}")
    print(f"Enabled:           {status['enabled']}")
    print(f"Last Evaluation:   {status.get('latest_evaluation_at', 'never')}")
    print(f"Events (24h):      {status['event_count_24h']}")
    print(f"Outcomes (24h):    {status['outcome_count_24h']}")
    print(f"Proposals:         {status['proposal_count']}")
    print(f"  Shadow:          {status['shadow_count']}")
    print(f"  Proposed:        {status['proposed_count']}")
    print(f"  Blocked:         {status['blocked_count']}")
    print(f"  Applied:         {status['approved_count']}")
    print(f"Active Promotion:   {status['allow_active_promotion']}")


def cmd_self_improvement_proposals(args):
    """List recent self-improvement proposals."""
    from .self_improvement import SelfImprovementStore
    limit = getattr(args, "limit", 20)
    store = SelfImprovementStore()
    proposals = store.list_proposals(limit=limit)
    if not proposals:
        print("No proposals found.")
        return
    status_filter = getattr(args, "status", None)
    if status_filter:
        proposals = [p for p in proposals if p.status.value == status_filter]
    print(f"--- Self-Improvement Proposals (limit={limit}) ---\n")
    for p in proposals:
        print(f"  [{p.status.value}] {p.proposal_id[:8]}  target={p.target.value}")
        print(f"    Before: {p.before} → After: {p.after}")
        print(f"    Reason: {p.reason[:80]}")
        print(f"    Evidence: {p.evidence_count} | Risk: {p.risk_level} | Key: {p.target_key}")
        print()


def cmd_self_improvement_evaluate(args):
    """Run one evaluation cycle (dry-run by default)."""
    from .self_improvement import SelfImprovementIntegration
    si_cfg = {}
    # Read config if available
    try:
        cfg = config_loader.ConfigLoader.load()
        si_cfg = cfg.get("self_improvement", {})
    except Exception:
        pass
    si = SelfImprovementIntegration(cfg=si_cfg)
    proposals = si.run_evaluation_cycle()
    if not proposals:
        print("No new proposals generated.")
    else:
        print(f"Generated {len(proposals)} proposal(s):")
        for p in proposals:
            print(f"  [{p.status.value}] {p.proposal_id[:8]}: {p.reason[:80]}")


def cmd_priority_explain(args):
    """Explain a specific candidate by ID."""
    from . import engine
    cid = getattr(args, "candidate_id", None)
    if not cid:
        print("Usage: helios priority explain <candidate_id>")
        return
    cfg = config_loader.ConfigLoader.load()
    eng = engine.HeliosEngine(cfg=cfg)
    rows = eng.summarizer._query_all(
        "SELECT candidate_id, title, category, severity, source FROM priority_candidates WHERE candidate_id = ?",
        (cid,),
    )
    scores = eng.summarizer._query_all(
        "SELECT final_score, explanation, urgency, importance, relevance, "
        "confidence, context_fit, actionability, novelty, safety, "
        "disruption_cost, staleness, annoyance, redundancy "
        "FROM priority_scores WHERE candidate_id = ?",
        (cid,),
    )
    decisions = eng.summarizer._query_all(
        "SELECT decision, route, reason FROM priority_decisions WHERE candidate_id = ?",
        (cid,),
    )
    eng.close()
    if not rows:
        print(f"Candidate {cid!r} not found.")
        return
    c = rows[0]
    s = scores[0] if scores else {}
    d = decisions[0] if decisions else {}
    print(f"--- Candidate: {c['title']} ---")
    print(f"ID:    {c['candidate_id']}")
    print(f"Type:  {c['source']} | Category: {c['category']} | Severity: {c['severity']}")
    if s:
        print(f"Score: {s['final_score']:.3f}")
        print(f"Dims:  urgency={s.get('urgency',0):.2f} importance={s.get('importance',0):.2f} "
              f"relevance={s.get('relevance',0):.2f} confidence={s.get('confidence',0):.2f}")
        print(f"       context_fit={s.get('context_fit',0):.2f} actionability={s.get('actionability',0):.2f} "
              f"novelty={s.get('novelty',0):.2f} safety={s.get('safety',0):.2f}")
        print(f"       disruption={s.get('disruption_cost',0):.2f} staleness={s.get('staleness',0):.2f} "
              f"annoyance={s.get('annoyance',0):.2f} redundancy={s.get('redundancy',0):.2f}")
        print(f"Explanation: {s.get('explanation', 'N/A')}")
    if d:
        print(f"Decision: {d.get('decision','?')} | Route: {d.get('route','?')} | Reason: {d.get('reason','?')}")


def main(argv: Optional[list[str]] = None):
    parser = argparse.ArgumentParser(prog="helios", description="Helios v6 Life Management Engine")
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ERROR")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("tick", help="Run one engine tick")
    sub.add_parser("brain", help="Run pattern-learning pass")
    sub.add_parser("daily", help="Generate daily briefings")
    sub.add_parser("shadow", help="Tick in shadow mode (no writes)")
    sub.add_parser("status", help="Show engine state summary")
    sub.add_parser("new-module", help="Scaffold a new module").add_argument("slug", help="Module slug (e.g. screen-time)")
    sub.add_parser("list-modules", help="List all discovered modules (read-only)")
    sub.add_parser("config-check", help="Privacy-safe health and config check")
    p_daemon = sub.add_parser("daemon", help="Run continuous daemon")
    p_daemon.add_argument("--interval", type=int, default=None, help="Tick interval seconds")

    # Priority Engine CLI
    p_pri = sub.add_parser("priority", help="Priority Engine introspection")
    pri_sub = p_pri.add_subparsers(dest="priority_command", required=True)
    pri_sub.add_parser("latest", help="Show latest tick summary")
    p_pri_recent = pri_sub.add_parser("recent", help="Summary for last N hours")
    p_pri_recent.add_argument("--hours", type=int, default=24, help="Hours to look back")
    p_pri_explain = pri_sub.add_parser("explain", help="Explain a candidate by ID")
    p_pri_explain.add_argument("candidate_id", help="Candidate UUID")

    # Self-Improvement CLI
    p_si = sub.add_parser("self-improvement", help="Self-improvement loop introspection")
    si_sub = p_si.add_subparsers(dest="si_command", required=True)
    si_sub.add_parser("status", help="Show self-improvement loop status")
    p_si_proposals = si_sub.add_parser("proposals", help="List recent proposals")
    p_si_proposals.add_argument("--limit", type=int, default=20, help="Max proposals to show")
    p_si_proposals.add_argument("--status", default=None, help="Filter by status (shadow/proposed/blocked)")
    p_si_eval = si_sub.add_parser("evaluate", help="Run one evaluation cycle")
    p_si_eval.add_argument("--dry-run", action="store_true", help="Dry run (always true in shadow mode)")

    args = parser.parse_args(argv)
    setup_logging(args.log_level)

    commands = {
        "tick": cmd_tick,
        "brain": cmd_brain,
        "daily": cmd_daily,
        "shadow": cmd_shadow,
        "daemon": cmd_daemon,
        "status": cmd_status,
        "new-module": cmd_new_module,
        "list-modules": cmd_list_modules,
        "config-check": cmd_config_check,
    }
    fn = commands.get(args.command)
    if args.command == "priority":
        pri_commands = {
            "latest": cmd_priority_latest,
            "recent": cmd_priority_recent,
            "explain": cmd_priority_explain,
        }
        fn = pri_commands.get(args.priority_command)
    elif args.command == "self-improvement":
        si_commands = {
            "status": cmd_self_improvement_status,
            "proposals": cmd_self_improvement_proposals,
            "evaluate": cmd_self_improvement_evaluate,
        }
        fn = si_commands.get(args.si_command)
    if fn:
        rc = fn(args)
        if isinstance(rc, int):
            sys.exit(rc)
    else:
        parser.print_help()
        sys.exit(1)


def cmd_new_module(args) -> int:
    """Generate a new Helios module from template."""
    import re
    from pathlib import Path as _P

    slug = args.slug
    if not re.match(r"^[a-z][a-z0-9_-]+$", slug):
        print(f"ERROR: '{slug}' not a valid slug (a-z, 0-9, hyphens, underscores)")
        return 1

    class_name = "".join(w.capitalize() for w in slug.replace("-", "_").split("_")) + "Module"
    module_name = slug.replace("-", " ").replace("_", " ").title()
    description = f"Tracks {module_name.lower()} data for Helios v6."

    modules_dir = _P(__file__).parent / "modules"
    # Support both .py and .j2 template files
    tmpl_path = modules_dir / "_template.py.j2"
    if not tmpl_path.exists():
        tmpl_path = modules_dir / "_template.py"
    if not tmpl_path.exists():
        print(f"ERROR: Template not found at {modules_dir / '_template.py.j2'}")
        return 1

    code = tmpl_path.read_text()
    code = code.replace("{{MODULE_NAME}}", module_name)
    code = code.replace("{{CLASS_NAME}}", class_name)
    code = code.replace("{{MODULE_SLUG}}", slug)
    code = code.replace("{{DESCRIPTION}}", description)

    out = modules_dir / f"{slug}.py"
    if out.exists():
        print(f"ERROR: {out} already exists")
        return 1

    out.write_text(code)
    print(f"Created: {out}")
    print()
    print("Add to config.yaml under modules:")
    print(f"  {slug}:")
    print(f"    enabled: true")
    print()
    print("Then: systemctl --user restart helios-v6")
    return 0


def cmd_list_modules(args) -> int:
    """List all discovered modules — pure read-only, no DB or engine."""
    import importlib
    import pkgutil
    from helios.modules.base import BaseMod

    modules_dir = Path(__file__).parent / "modules"
    discovered = {}

    for importer, modname, ispkg in pkgutil.iter_modules([str(modules_dir)]):
        if modname.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"helios.modules.{modname}")
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if (isinstance(attr, type) and issubclass(attr, BaseMod)
                        and attr is not BaseMod):
                    manifest = getattr(attr, "MODULE_MANIFEST", {})
                    discovered[modname] = attr
        except Exception:
            pass

    print(f"\n{len(discovered)} module(s) discovered:\n")
    print(f"{'Name':22} {'Version':8} {'Priority':8} Description")
    print("-" * 75)
    for name, cls in sorted(discovered.items()):
        m = getattr(cls, "MODULE_MANIFEST", {})
        print(f"{name:22} {m.get('version','?'):8} {m.get('priority','?'):<8} {m.get('description','')}")
    return 0


def cmd_config_check(args) -> int:
    """Privacy-safe health and configuration check — no DB writes, no migrations."""
    import importlib
    import pkgutil
    from helios.modules.base import BaseMod

    issues: list[str] = []
    checks: list[str] = []

    # Resolve paths via HELIOS_BASE (same as the state module)
    base_dir = _helios_base()
    db_path = os.path.join(base_dir, "helios_v6.db")
    data_dir = os.path.join(base_dir, "data")

    # 1. Config loads
    try:
        cfg = config_loader.ConfigLoader.load()
        checks.append("Config loads successfully")
    except Exception as exc:
        issues.append(f"Config load failed: {exc}")
        print(f"\nConfig load failed: {exc}")
        return 1

    # 2. DB is accessible (read-only, no migration, respects HELIOS_BASE)
    conn = None
    if not os.path.exists(db_path):
        issues.append(f"Database file not found: {db_path}")
    else:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.execute("PRAGMA query_only = ON")
            # Check timeline_events schema
            cols = conn.execute("PRAGMA table_info(timeline_events)").fetchall()
            col_names = {c[1] for c in cols}
            required = {"ts", "event_type", "source_module", "importance", "summary", "date_key"}
            missing = required - col_names
            if missing:
                issues.append(f"timeline_events missing columns: {sorted(missing)}")
            else:
                checks.append(f"timeline_events schema OK ({len(cols)} columns)")

            # Check timeline_sessions
            try:
                sess_cols = conn.execute("PRAGMA table_info(timeline_sessions)").fetchall()
                checks.append(f"timeline_sessions table exists ({len(sess_cols)} columns)")
            except Exception:
                issues.append("timeline_sessions table missing")

            cnt = conn.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0]
            checks.append(f"timeline_events has {cnt} rows")
        except Exception as exc:
            issues.append(f"Database read failed: {exc}")
        finally:
            if conn:
                conn.close()

    # 3. Module discovery (read-only, no DB, no engine)
    try:
        modules_dir = Path(__file__).parent / "modules"
        discovered = {}
        for imp, modname, ispkg in pkgutil.iter_modules([str(modules_dir)]):
            if modname.startswith("_"):
                continue
            try:
                mod = importlib.import_module(f"helios.modules.{modname}")
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    if (isinstance(attr, type) and issubclass(attr, BaseMod)
                            and attr is not BaseMod):
                        discovered[modname] = attr
            except Exception:
                pass
        enabled = cfg.modules if hasattr(cfg, 'modules') else {}
        enabled_count = sum(1 for v in enabled.values() if isinstance(v, dict) and v.get("enabled", True))
        checks.append(f"{len(discovered)} module(s) discovered, {enabled_count} enabled in config")
    except Exception as exc:
        issues.append(f"Module discovery failed: {exc}")

    # 4. Privacy check — no raw coordinates in recent timeline events (read-only)
    conn2 = None
    try:
        if os.path.exists(db_path):
            conn2 = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn2.execute("PRAGMA query_only = ON")
            suspicious = conn2.execute(
                "SELECT COUNT(*) FROM timeline_events "
                "WHERE metadata LIKE '%lat%' OR metadata LIKE '%lon%'"
            ).fetchone()[0]
            if suspicious > 0:
                issues.append(f"{suspicious} timeline event(s) may contain lat/lon in metadata")
            else:
                checks.append("No raw coordinates found in timeline metadata")
    except Exception:
        pass  # Non-critical
    finally:
        if conn2:
            conn2.close()

    # 5. Key directories (respect HELIOS_BASE)
    if os.path.exists(data_dir):
        checks.append(f"Data directory exists: {data_dir}")
    else:
        issues.append(f"Data directory missing: {data_dir}")

    # Report
    print("\n=== Helios v6 Config & Health Check ===\n")
    for c in checks:
        print(f"  {c}")
    for i in issues:
        print(f"  {i}")
    print()
    if issues:
        print(f"{len(issues)} issue(s) found — see above")
        return 1
    else:
        print("All checks passed")
        return 0


if __name__ == "__main__":
    main()