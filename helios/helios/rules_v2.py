"""Helios v5 — Rules engine v2.

Evaluates rules against context dict.
Supports simple expression strings like:
  calendar.busy_today == true
  weather.temp > 25
  gaming.duration_secs > 1800
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("helios.rules")


class RulesEngine:
    def __init__(self, db):
        self.db = db

    def evaluate(self, context: dict[str, Any]) -> list[dict]:
        """Evaluate all enabled rules against context, return hits."""
        rules = self.db.get_rules(enabled=True)
        hits: list[dict] = []
        now = datetime.now(timezone.utc).isoformat()
        for rule in rules:
            if not self._check_cooldown(rule):
                continue
            expr = rule.get("condition") or ""
            if not expr:
                continue
            try:
                if self._eval_expr(expr, context):
                    hits.append(dict(rule))
                    self._mark_triggered(rule["slug"])
            except Exception as exc:
                log.debug("Rule %s eval error: %s", rule["slug"], exc)
        return hits

    def _check_cooldown(self, rule: dict) -> bool:
        """Return False if rule is still in cooldown."""
        last = rule.get("last_triggered")
        secs = rule.get("cooldown_secs", 0) or 0
        if not last or not secs:
            return True
        try:
            t = datetime.fromisoformat(last)
            return (datetime.now(timezone.utc) - t).total_seconds() >= secs
        except Exception:
            return True

    def _mark_triggered(self, slug: str) -> None:
        """Update rules.last_triggered in DB so cooldown works across ticks."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            self.db._execute(
                "UPDATE rules SET last_triggered = ? WHERE slug = ?",
                (now, slug)
            )
            with self.db._conn() as c:
                c.commit()
        except Exception as exc:
            log.warning("Failed to mark rule %s triggered: %s", slug, exc)

    def _eval_expr(self, expr: str, context: dict) -> bool:
        """Naive but safe expression evaluator."""
        # Normalize common rule keywords to valid Python equivalents first
        expr = self._normalize_expr(expr)

        # Extract module.key references
        # Replace module.key with actual values from context dict
        def replacer(m: re.Match) -> str:
            parts = m.group(0).split(".")
            mod = parts[0]
            key = parts[1] if len(parts) > 1 else ""
            val = context.get(mod, {})
            if isinstance(val, dict):
                v = val.get(key)
            else:
                v = None
            if v is None:
                return json.dumps("")
            if isinstance(v, bool):
                return "True" if v else "False"
            if isinstance(v, (int, float)):
                return str(v)
            return json.dumps(v)

        # Match word.word patterns (module.key)
        expr_clean = re.sub(r"[a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*", replacer, expr)
        try:
            return bool(eval(expr_clean, {"__builtins__": {}}, {}))
        except Exception:
            return False

    def _normalize_expr(self, expr: str) -> str:
        """Normalize common rule keywords to valid Python equivalents."""
        # Replace keyword booleans
        expr = expr.replace(" AND ", " and ").replace(" OR ", " or ").replace(" NOT ", " not ")
        # Replace standalone true/false
        expr = re.sub(r"\btrue\b", "True", expr)
        expr = re.sub(r"\bfalse\b", "False", expr)
        return expr
