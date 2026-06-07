"""Tests for Priority Engine CLI commands."""

import pytest
from unittest.mock import MagicMock, patch
from helios.main import cmd_priority_latest, cmd_priority_recent, cmd_priority_explain


class FakeArgs:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


# Since engine and config_loader are now lazy-imported inside command handlers,
# we must patch them at their module source: helios.engine and helios.config_loader.
# cmd_priority_* functions do: from . import engine; cfg = config_loader.ConfigLoader.load()
# So we need to patch both the engine and the config loader.


class TestPriorityCLI:
    @patch("helios.config_loader.ConfigLoader.load")
    @patch("helios.engine.HeliosEngine")
    def test_cmd_priority_latest_prints_summary(self, MockEng, mock_cfg, capsys):
        mock_cfg.return_value = MagicMock()
        eng = MagicMock()
        eng.summarizer.generate.return_value = {
            "totals": {"generated": 3, "scored": 3, "selected": 1, "suppressed": 1},
            "top_candidates": [
                {"title": "Test Alert", "score": 0.85, "decision": "select_notify",
                 "explanation": "top factors: urgency=0.80 | score: high"},
            ],
            "generated_at": "2026-05-16T14:00:00+00:00",
        }
        MockEng.return_value = eng
        cmd_priority_latest(FakeArgs())
        out = capsys.readouterr().out
        assert "Latest Priority Tick" in out
        assert "Generated: 3" in out
        assert "Test Alert" in out
        eng.close.assert_called_once()

    @patch("helios.config_loader.ConfigLoader.load")
    @patch("helios.engine.HeliosEngine")
    def test_cmd_priority_recent_outputs_json(self, MockEng, mock_cfg, capsys):
        mock_cfg.return_value = MagicMock()
        eng = MagicMock()
        eng.summarizer.generate.return_value = {"totals": {"generated": 5}}
        MockEng.return_value = eng
        cmd_priority_recent(FakeArgs(hours=12))
        out = capsys.readouterr().out
        assert '"generated": 5' in out
        eng.close.assert_called_once()

    @patch("helios.config_loader.ConfigLoader.load")
    @patch("helios.engine.HeliosEngine")
    def test_cmd_priority_explain_found(self, MockEng, mock_cfg, capsys):
        mock_cfg.return_value = MagicMock()
        eng = MagicMock()
        eng.summarizer._query_all.side_effect = [
            [{"candidate_id": "abc", "title": "My Alert", "source": "rules_v2",
              "category": "home", "severity": "warning"}],
            [{"final_score": 0.75, "explanation": "top factors: urgency=0.80 | score: high",
              "urgency": 0.8, "importance": 0.7, "relevance": 0.6, "confidence": 0.5,
              "context_fit": 0.4, "actionability": 0.3, "novelty": 0.2, "safety": 0.9,
              "disruption_cost": 0.1, "staleness": 0.1, "annoyance": 0.1, "redundancy": 0.1}],
            [{"decision": "select_notify", "route": "channel", "reason": "above threshold"}],
        ]
        MockEng.return_value = eng
        cmd_priority_explain(FakeArgs(candidate_id="abc"))
        out = capsys.readouterr().out
        assert "My Alert" in out
        assert "0.750" in out
        assert "select_notify" in out
        eng.close.assert_called_once()

    @patch("helios.config_loader.ConfigLoader.load")
    @patch("helios.engine.HeliosEngine")
    def test_cmd_priority_explain_not_found(self, MockEng, mock_cfg, capsys):
        mock_cfg.return_value = MagicMock()
        eng = MagicMock()
        eng.summarizer._query_all.side_effect = [[], [], []]
        MockEng.return_value = eng
        cmd_priority_explain(FakeArgs(candidate_id="missing"))
        out = capsys.readouterr().out
        assert "not found" in out
        eng.close.assert_called_once()

    @patch("helios.engine.HeliosEngine")
    def test_cmd_priority_explain_no_id(self, MockEng, capsys):
        # No candidate_id — should print usage
        cmd_priority_explain(FakeArgs())
        out = capsys.readouterr().out
        assert "Usage" in out