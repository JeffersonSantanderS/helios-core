# Helios Channel Adapter System

## Architecture

Channels are Helios's normalized outbound delivery layer. Modules create **event objects** and send them through a **ChannelRouter**. The router dispatches each event to all enabled channels. MatrixPusher is the underlying Matrix transport — channels wrap it.

```text
Module (e.g., engine.py, dispatcher.py)
    │
    ▼
AlertEvent / BriefingEvent / CheckinEvent / StatusEvent
    │
    ▼
ChannelRouter.send(event)
    │
    ├──► MatrixChannel  →  MatrixPusher  →  Matrix API (curl)
    ├──► LogChannel     →  Python logging + JSONL file
    ├──► (future) TelegramChannel
    └──► (future) WebhookChannel
```

## Migration Status

| Component | Status | Details |
|-----------|--------|---------|
| Engine outbound (`_emit_*`) | ✅ Complete | All `_emit_*` helpers use ChannelRouter primary |
| PriorityDispatcher | ✅ Complete | ChannelRouter primary, `matrix_pusher` fallback |
| AlertDispatcher | ✅ Complete | ChannelRouter primary, `matrix_pusher` fallback |
| ActionEngine | ✅ Complete | ChannelRouter first, inline MatrixPusher fallback |
| MoodHandler | ✅ Complete | CheckinEvent audit via channels; raw curl for Matrix reactions (intentional) |
| Dashboard | ✅ Complete | Reads sanitized state and channel logs |
| DMListener | ⬜ Separate | Inbound only, not part of outbound migration |

### Intentional Exceptions

- **MoodHandler raw curl**: Matrix reaction buttons (emoji scale) require `event_id` from the message response. This is.Matrix-specific and cannot be abstracted into a channel. The handler emits a `CheckinEvent` audit event through channels, then uses raw curl for the message + reactions.

- **Shadow mode**: Engine's `run_shadow()` still monkey-patches `self.matrix_pusher.push`/`push_dm` as a safety net. ChannelRouter shadow mode is the primary suppression mechanism. Both are kept for belt-and-suspenders reliability.

## Event Types

| Event | Purpose | Typical Priority |
|-------|---------|-----------------|
| `AlertEvent` | Rule hits, priority candidates, proactive intelligence | 1-3 |
| `BriefingEvent` | Morning/evening briefings | 1 |
| `CheckinEvent` | Mood check-ins, hydration reminders | 1-2 |
| `StatusEvent` | Healing messages, digests, system status | 1-2 |
| `BaseEvent` | Generic message fallback | 1 |

All events share: `event_type`, `title`, `message`, `priority`, `embed`, `category`, `source`.

- `AlertEvent` adds: `severity` (info/warning/critical/success/system), `slug`, `rule_description`
- `BriefingEvent` adds: `briefing_type` (morning/evening)
- `CheckinEvent` adds: `checkin_type` (mood/hydration/etc), `html_body`, `prompt_options`, `metadata`

## Channel Interface

Every channel implements `BaseChannel` with per-event-type methods:

| Method | Purpose |
|--------|---------|
| `send_alert(event)` | Deliver an alert (DM for priority >= 3, channel otherwise) |
| `send_briefing(event)` | Deliver a briefing |
| `send_checkin(event)` | Deliver a check-in prompt (prefers DM) |
| `send_status(event)` | Deliver a system status message |
| `send_message(event)` | Deliver a generic message |

Each method checks `self.enabled` first, then delegates to `_send_*_impl`. Shadow mode is handled by `BaseChannel.send()` which returns a `shadow_suppressed` result without calling `_send_*_impl`.

## Delivery Guarantee

- **Primary path**: Module → `_emit_*()` → `ChannelRouter.send()` → all enabled channels
- **Fallback path**: If `ChannelRouter` is `None` or all channels fail → direct `matrix_pusher.push()`/`push_dm()`
- **No duplicates**: When ChannelRouter succeeds, `matrix_pusher` is NOT called. When it fails, `matrix_pusher` delivers exactly once.
- **Audit trail**: `LogChannel` always captures events for the JSONL audit log, even in shadow mode.

## Priority Routing

Priority-based routing is preserved across both paths:

| Priority | ChannelRouter (MatrixChannel) | Fallback (MatrixPusher) |
|----------|-------------------------------|------------------------|
| ≥ 3 (critical) | DM notification | `push_dm()` |
| ≥ 2 (important) | Channel + DM (two events) | `push()` + `push_dm()` |
| < 2 (routine) | Channel notification | `push()` |

## Configuration

```yaml
channels:
  matrix:
    enabled: true
    # Inherits from top-level `matrix:` config
    # (homeserver, access_token, room, dm_user, etc.)
  log:
    enabled: true
    jsonl_path: "~/.hermes/helios/data/channel_log.jsonl"
    log_level: "info"

priority:
  enabled: true
  mode: shadow  # shadow = suppress Matrix, only log
```

## Shadow Mode

When `priority.mode: shadow`:
1. `ChannelRouter.shadow = True` — all non-LogChannel channels return `shadow_suppressed`
2. Engine's `run_shadow()` also monkey-patches `self.matrix_pusher.push`/`push_dm` to no-ops as a safety net
3. `LogChannel` still captures all events for JSONL audit
4. No Matrix messages are sent. No duplicates.

## Remaining `matrix_pusher` References

`self.matrix_pusher` still exists in these locations as intentional fallback:

1. **AlertDispatcher._send_legacy_matrix()**: Fallback when ChannelRouter is unavailable or fails
2. **PriorityDispatcher._dispatch_one()**: Fallback for `matrix_dm` and `matrix_channel` routes
3. **Engine._emit()**: Fallback when `self.channels` is None or all channels fail
4. **Engine.run_shadow()**: Monkey-patches push/push_dm as shadow-mode safety net
5. **MoodHandler**: Raw curl path for Matrix reaction emoji buttons

These are NOT bugs — they are the deliberate fallback layer that ensures Matrix delivery still works if ChannelRouter has issues.