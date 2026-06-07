"""Helios v5 — File watcher for event-driven module ticks.

Watches Obsidian vault, health data, mac_bridge, and collector outputs.
Maps file changes to module names and queues targeted ticks.
Runs alongside the 5-minute full tick — not a replacement.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

log = logging.getLogger("helios.watcher")

# --- File path → module name mappings ---
WATCH_PATHS: dict[str, list[str]] = {
    "obsidian_vault": ["notes", "calendar", "tasks"],
    "health_data": ["health"],
    "mac_bridge.json": ["focus"],
    "focus_state.json": ["focus"],
    "spotify_state.json": ["spotify"],
    "idle_state.json": ["focus"],
    "icloud_location_sync.json": ["location"],
}


class HeliosEventHandler(FileSystemEventHandler):
    """Debounced file change handler that queues module triggers."""

    def __init__(self, callback: Callable[[str], None], cooldown: float = 30.0):
        self._callback = callback
        self._cooldown = cooldown
        self._last_triggered: dict[str, float] = {}

    def _map_to_modules(self, path: str) -> list[str]:
        """Map a changed file path to module names."""
        modules: list[str] = []
        for pattern, mods in WATCH_PATHS.items():
            if pattern in path:
                modules.extend(mods)
        return list(set(modules)) or []

    def on_modified(self, event: FileSystemEvent) -> None:
        src = event.src_path
        if not src or event.is_directory:
            return
        modules = self._map_to_modules(src)
        now = time.time()
        for mod in modules:
            last = self._last_triggered.get(mod, 0)
            if now - last < self._cooldown:
                continue
            self._last_triggered[mod] = now
            self._callback(mod)
            log.debug("File change -> module: %s (from %s)", mod, src)


class FileWatcher:
    """Manages watchdog Observer for event-driven Helios ticks."""

    def __init__(
        self,
        obsidian_vault: str = "",
        health_data_dir: str = "",
        collector_data_dir: str = "",
        cooldown: float = 30.0,
    ):
        self._paths: dict[str, str] = {}
        if obsidian_vault:
            self._paths["obsidian_vault"] = obsidian_vault
        if health_data_dir:
            self._paths["health_data"] = health_data_dir
        if collector_data_dir:
            self._paths["collector_data"] = collector_data_dir

        self._observer: Optional[Observer] = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._running = False
        self._cooldown = cooldown

    @property
    def event_queue(self) -> queue.Queue[str]:
        return self._queue

    def start(self) -> None:
        """Start watching configured paths in a background thread."""
        if self._running:
            return
        self._observer = Observer()

        for path_key, path in self._paths.items():
            p = Path(path)
            if not p.exists():
                log.warning("Watch path does not exist: %s (%s)", path, path_key)
                continue

            handler = HeliosEventHandler(self._on_module_trigger, cooldown=self._cooldown)
            self._observer.schedule(handler, str(p), recursive=True)
            log.info("Watching: %s (%s)", path, path_key)

        if not self._observer._handlers:
            log.warning("No valid watch paths; watcher idle")
            return

        self._running = True
        self._observer.start()

    def stop(self) -> None:
        self._running = False
        if self._observer:
            self._observer.stop()
            try:
                self._observer.join(timeout=5)
            except RuntimeError:
                # Observer thread was never started (no valid watch paths)
                pass

    def _on_module_trigger(self, module_name: str) -> None:
        self._queue.put(module_name)

    def drain(self) -> list[str]:
        """Return all pending module triggers (non-blocking)."""
        modules: list[str] = []
        while True:
            try:
                modules.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return list(set(modules))
