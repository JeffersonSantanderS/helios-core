"""Helios v5 — LLM Bridge.

Async handoff: engine writes llm_requests rows; bridge polls and processes.
Calls DeepSeek via OpenAI-compatible API.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger("helios.llm_bridge")


class _DictConfigWrapper:
    """Wraps a plain dict to support ConfigLoader-style segmented-key .get().

    When ``cfg.get("llm", "base_url", default="")`` is called on a plain dict,
    ``dict.get()`` raises ``TypeError`` because it only accepts a single key
    and no keyword arguments.  This wrapper splits multi-arg calls into nested
    dict lookups so callers can work with either a ``ConfigLoader`` or a plain
    dict transparently.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def get(self, *keys: str, default: Any = None) -> Any:
        if len(keys) == 1 and isinstance(keys[0], str) and "." in keys[0]:
            keys = tuple(keys[0].split("."))
        d: Any = self._data
        for k in keys:
            if isinstance(d, dict) and k in d:
                d = d[k]
            else:
                return default
        return d

    def __repr__(self) -> str:
        return f"_DictConfigWrapper({list(self._data.keys())})"


class LLMBridge:
    def __init__(self, db, cfg: Optional[Any] = None):
        self.db = db
        # Accept either a ConfigLoader (multi-arg .get()) or a plain dict.
        # Plain dicts get wrapped so segmented-key .get() calls work.
        if isinstance(cfg, dict):
            self.cfg: Any = _DictConfigWrapper(cfg)
        else:
            self.cfg = cfg
        self._client: Any = None
        self._init_client()

    def _init_client(self) -> None:
        try:
            import openai
        except Exception:
            log.warning("openai package not installed; LLM bridge disabled")
            return
        base_url = self.cfg.get("llm", "base_url", default="") or os.environ.get("HELIOS_LLM_URL", "")
        api_key = self.cfg.get("llm", "api_key", default="") or os.environ.get("HELIOS_LLM_KEY", "")
        if not base_url or not api_key:
            log.warning("LLM URL or key missing; LLM bridge disabled")
            return
        # ollama uses base_url + dummy key; v2 OpenAI client needs explicit args
        try:
            from openai import OpenAI
            self._client = OpenAI(base_url=base_url, api_key=api_key)
        except ImportError:
            import openai
            self._client = openai.OpenAI(base_url=base_url, api_key=api_key)

    def process_pending(self, limit: int = 5) -> list[dict]:
        """Poll DB for pending requests, process up to limit, return results."""
        daily_cap = self.cfg.get("llm", "daily_cap", default=20)
        if self.db.today_llm_call_count() >= daily_cap:
            log.info("Daily LLM cap reached; skipping")
            return []

        reqs = self.db.get_pending_llm_requests(limit=limit)
        results: list[dict] = []
        for req in reqs:
            result = self._process_one(req)
            results.append(result)
        return results

    def _process_one(self, req: dict) -> dict:
        req_id = req["id"]
        self.db.update_llm_request(req_id, status="processing")
        if self._client is None:
            self.db.update_llm_request(req_id, status="failed", error="LLM client not initialized")
            return {"id": req_id, "status": "failed", "error": "LLM client not initialized"}

        try:
            # Build prompt from context keys
            context_keys = json.loads(req.get("context_keys") or "[]")
            prompt = self._build_prompt(context_keys, req.get("prompt_template"))
            model = self.cfg.get("llm", "model", default="deepseek-v4-flash")
            max_tokens = req.get("max_tokens", 512)
            resp = self._client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
            )
            text = resp.choices[0].message.content or ""
            self.db.update_llm_request(req_id, status="done", result=text, model_used=model)
            return {"id": req_id, "status": "done", "result": text}
        except Exception as exc:
            log.exception("LLM request %s failed", req_id)
            self.db.update_llm_request(req_id, status="failed", error=str(exc))
            return {"id": req_id, "status": "failed", "error": str(exc)}

    @staticmethod
    def _build_prompt(context_keys: list[str], template: Optional[str]) -> str:
        # Simple context assembly — future: read from DB
        body = "\n".join(f"- {k}" for k in context_keys)
        if template:
            return template.replace("{{context}}", body)
        return f"Helios insight request. Available context:\n{body}\n\nGenerate a concise summary."