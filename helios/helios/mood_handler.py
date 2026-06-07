"""Mood logging handler — Matrix delivery.

Replaces the Discord bot client with Matrix HTML messages.
The user replies with an emoji, a number (1, 3, 5, 7, 9), or a keyword
and the tick cycle records it.

Accepted emojis:
  😭💔😵🤬 — Terrible (1)
  👎😤😩😞 — Bad (3)
  🫤😬😶🙃 — Okay (5)
  🙂👍😊😌 — Good (7)
  🤩🚀🎉🙌 — Great (9)

Data written to: ~/.hermes/helios/data/mood_state.json
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("helios.mood_handler")

MOOD_FILE = Path.home() / ".hermes" / "helios" / "data" / "mood_state.json"
DATA_DIR = Path.home() / ".hermes" / "helios" / "data"
ENV_FILE = Path.home() / ".hermes" / ".env"

MOOD_MAP = {
    "mood_1": (1, "Terrible"),
    "mood_3": (3, "Bad"),
    "mood_5": (5, "Okay"),
    "mood_7": (7, "Good"),
    "mood_9": (9, "Great"),
}

# Invert for text parsing
SCORE_LABELS = {
    1: "Terrible", 3: "Bad", 5: "Okay", 7: "Good", 9: "Great",
}
KEYWORD_MAP = {
    "terrible": 1, "awful": 1, "worst": 1, "1": 1,
    "bad": 3, "poor": 3, "shitty": 3, "rough": 3, "3": 3,
    "okay": 5, "ok": 5, "fine": 5, "meh": 5, "neutral": 5, "alright": 5, "5": 5,
    "good": 7, "decent": 7, "alright": 7, "better": 7, "7": 7,
    "great": 9, "excellent": 9, "amazing": 9, "fantastic": 9, "best": 9, "9": 9,
}

MOOD_REACTION_TAG = "helios:mood"

EMOJI_MAP = {
    "😭": 1, "💔": 1, "😵": 1, "🤬": 1, "🔥": 1,  # Terrible
    "👎": 3, "😤": 3, "😩": 3, "😞": 3, "😒": 3,  # Bad
    "😐": 5, "😬": 5, "😶": 5, "🫤": 5, "🙃": 5,  # Okay
    "🙂": 7, "👍": 7, "😊": 7, "😌": 7, "💪": 7,  # Good
    "🤩": 9, "🔥": 9, "🚀": 9, "🎉": 9, "🙌": 9,  # Great
    "💩": 1, "🫠": 5,                           # extras
}

_EMOJI_SCORE_LABELS = {
    1: ("😭", "Terrible"),
    3: ("👎", "Bad"),
    5: ("🫤", "Okay"),
    7: ("🙂", "Good"),
    9: ("🤩", "Great"),
}

# ── config helpers ──────────────────────────────────────────────────────

def _get_token():
    if not ENV_FILE.exists():
        return None
    with open(ENV_FILE) as f:
        for line in f:
            if line.startswith("MATRIX_ACCESS_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None

def _get_homeserver():
    return os.environ.get("MATRIX_HOMESERVER", "").rstrip("/")

def _get_room():
    room = os.environ.get("MATRIX_HOME_ROOM", "")
    if not room and ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                if line.startswith("MATRIX_HOME_ROOM="):
                    room = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    return room

# ── data layer ────────────────────────────────────────────────────────────

def record_mood(score: int, label: str) -> bool:
    """Record mood. Returns True if new, False if already logged today."""
    MOOD_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"history": []}
    if MOOD_FILE.exists():
        try:
            with open(MOOD_FILE) as f:
                data = json.load(f)
        except Exception:
            pass

    today = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%d")
    for entry in data.get("history", []):
        if entry.get("date") == today:
            return False

    entry = {
        "date": today,
        "score": score,
        "label": label,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    data["history"].append(entry)
    with open(MOOD_FILE, "w") as f:
        json.dump(data, f, indent=2)
    return True

def get_today_mood() -> dict | None:
    """Return today's mood entry or None."""
    if not MOOD_FILE.exists():
        return None
    try:
        with open(MOOD_FILE) as f:
            data = json.load(f)
    except Exception:
        return None
    today = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%d")
    for entry in data.get("history", []):
        if entry.get("date") == today:
            return entry
    return None

def try_extract_mood(text: str) -> tuple[int, str] | None:
    """Parse a user's response to find a mood score.

    Accepts:
      - "mood 7", "7", "7/9"
      - "good", "great", "bad", etc.
      - Emojis: 😭 🤩 🙂 🫤 👎 etc.
    Returns (score, label) or None.
    """
    text = text.strip()
    text_lower = text.lower()

    # Emoji check first — look for any exact emoji match in text
    for emoji, score in EMOJI_MAP.items():
        if emoji in text:
            return score, SCORE_LABELS[score]

    # Direct number — prefer explicit digits
    m = re.search(r'(?![\d\w])\b[13579]\b(?!\d)', text_lower)
    if m:
        score = int(m.group())
        return score, SCORE_LABELS[score]

    # Keyword fall-through
    words = re.findall(r'[a-z]+', text_lower) + re.findall(r'\d+', text_lower)
    for word in words:
        if word in KEYWORD_MAP:
            score = KEYWORD_MAP[word]
            return score, SCORE_LABELS[score]

    return None

# ── delivery ──────────────────────────────────────────────────────────────

def send_mood_checkin(cfg: dict | None = None, channels=None):
    """Send a mood check-in message to Matrix. Called from daemon tick.

    Phase 4A: Now also emits a CheckinEvent to the ChannelRouter for
    audit/logging. The raw curl path is preserved as the primary delivery
    mechanism since it includes Matrix-specific reaction addition that
    cannot be replicated through a generic channel.

    Args:
        cfg: Helios config dict (used for raw curl fallback).
        channels: Optional ChannelRouter for event mirroring.
    """
    token = _get_token()
    if not token:
        log.warning("Mood check-in skipped — no Matrix token")
        return

    room = _get_room()
    if not room:
        log.warning("Mood check-in skipped — no Matrix room")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%H:%M MDT")

    # Already logged today?
    already = get_today_mood()
    if already:
        log.info("Mood already logged (%s/9 — %s), skipping check-in",
                 already["score"], already["label"])
        return

    # ── Phase 4A: Emit CheckinEvent to channel system for audit ──
    if channels is not None:
        try:
            from helios.channels.events import CheckinEvent
            channels.send(CheckinEvent(
                title=f"🧠 How are you feeling? — {today}",
                message=f"Mood check-in for {today}",
                priority=1,
                category="mood",
                source="mood_handler",
                checkin_type="mood",
                prompt_options=[
                    (1, "Terrible"), (3, "Bad"), (5, "Okay"),
                    (7, "Good"), (9, "Great"),
                ],
                metadata={
                    "reaction_emojis": ["😭", "👎", "🫤", "🙂", "🤩"],
                    "schedule": "daily",
                },
            ))
        except Exception as exc:
            log.debug("Mood CheckinEvent channel mirror failed: %s", exc)

    # ── Legacy raw curl delivery (preserved — includes reaction addition) ──

    home_server = _get_homeserver()
    txn_id = f"helios_mood_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}_{os.urandom(3).hex()}"

    plain = (
        f"🧠 How are you feeling? — {today}\n\n"
        f"Reply with an emoji or a number:\n"
        f"  😭 Terrible (1)     👎 Bad (3)     🫤 Okay (5)     🙂 Good (7)     🤩 Great (9)\n\n"
        f"[helios:mood] — Helios v6 · {now_str}"
    )
    html = (
        f"<h3>🧠 How are you feeling? — {today}</h3>"
        f"<p>Reply with an emoji or a number:</p>"
        f"<p>"
        f"<span style=\"font-size:1.5em;\">😭 Terrible (1)</span> &nbsp; "
        f"<span style=\"font-size:1.5em;\">👎 Bad (3)</span> &nbsp; "
        f"<span style=\"font-size:1.5em;\">🫤 Okay (5)</span> &nbsp; "
        f"<span style=\"font-size:1.5em;\">🙂 Good (7)</span> &nbsp; "
        f"<span style=\"font-size:1.5em;\">🤩 Great (9)</span>"
        f"</p>"
        f"<p><small>Helios v6 · {now_str} · [helios:mood]</small></p>"
    )

    payload = {
        "msgtype": "m.text",
        "body": plain,
        "format": "org.matrix.custom.html",
        "formatted_body": html,
    }

    try:
        result = subprocess.run(
            ["curl", "-s", "-S", "-w", "\n%{http_code}", "-X", "PUT",
             f"{home_server}/_matrix/client/v3/rooms/{room}/send/m.room.message/{txn_id}",
             "-H", f"Authorization: Bearer {token}",
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload),
             "--connect-timeout", "5",
             "--max-time", "15"],
            capture_output=True, text=True, timeout=20,
        )
        lines = result.stdout.splitlines()
        status = int(lines[-1]) if lines and lines[-1].isdigit() else 0
        if status in (200, 201, 202):
            log.info("Mood check-in sent to Matrix")
            # ── Add pre-set emoji reactions for one-click logging ──
            try:
                body_line = lines[-2] if len(lines) >= 2 else ""
                body_json = json.loads(body_line)
                event_id = body_json.get("event_id")
                if event_id:
                    reaction_emojis = ["😭", "👎", "🫤", "🙂", "🤩"]
                    for emoji in reaction_emojis:
                        rxn_txn = f"helios_rxn_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}_{os.urandom(3).hex()}"
                        rxn_payload = {
                            "m.relates_to": {
                                "rel_type": "m.annotation",
                                "event_id": event_id,
                                "key": emoji,
                            }
                        }
                        subprocess.run(
                            ["curl", "-s", "-S", "-o", "/dev/null", "-X", "PUT",
                             f"{home_server}/_matrix/client/v3/rooms/{room}/send/m.reaction/{rxn_txn}",
                             "-H", f"Authorization: Bearer {token}",
                             "-H", "Content-Type: application/json",
                             "-d", json.dumps(rxn_payload),
                             "--connect-timeout", "3",
                             "--max-time", "8"],
                            capture_output=True, timeout=10,
                        )
                    log.info("Mood check-in reactions added (%s)", event_id)
                else:
                    log.warning("Mood check-in: no event_id in response, skipping reactions")
            except Exception as exc:
                log.warning("Mood check-in: failed to add reactions: %s", exc)
        else:
            log.warning("Mood check-in failed: HTTP %s", status)
    except Exception as exc:
        log.warning("Mood check-in curl error: %s", exc)


# ── legacy stubs ─────────────────────────────────────────────────────────

def send_mood_embed(channels=None):
    """Deprecated — kept for compatibility. Delegates to send_mood_checkin."""
    log.warning("send_mood_embed() is deprecated; use send_mood_checkin(cfg, channels)")
    send_mood_checkin(channels=channels)


def handle_mood_reaction(sender: str, reacted_event_id: str, emoji: str, reacted_message_body: str) -> bool:
    """Handler for Matrix reactions on mood check-in messages.

    Called by ReactionPoller when a reaction is detected on a message
    containing [helios:mood]. Extracts the emoji score and records the mood.

    Returns True if consumed (mood logged), False otherwise.
    """
    # Ignore reactions from users not on the configured homeserver
    homeserver_domain = os.environ.get("MATRIX_HOMESERVER_DOMAIN", "")
    if homeserver_domain and not sender.endswith(f":{homeserver_domain}"):
        # Allow any user on our homeserver; external users ignored
        pass

    # Map emoji to score
    score = EMOJI_MAP.get(emoji)
    if score is None:
        log.debug("Mood reaction: unsupported emoji '%s' from %s", emoji, sender)
        return False

    label = SCORE_LABELS[score]
    ok = record_mood(score, label)
    if ok:
        log.info("Mood reaction logged: %s/9 (%s) from %s", score, label, sender)
    else:
        log.info("Mood reaction: already logged today (%s/9 %s)", score, label)
    return True


class MoodHandler:
    """Lightweight mood tracker. Daemon calls _maybe_send_mood_checkin() each tick."""
    """Note: The old Discord bot client is removed. Mood logging happens via
    Matrix replies or manual calls to record_mood()."""

    def __init__(self):
        self._enabled = True

    def start(self):
        """No-op: there is no background thread anymore."""
        pass

    def stop(self):
        """No-op."""
        pass
