"""The capture service loop.

Each tick:
  1. read foreground window + idle time
  2. honor the blocklist (pause on sensitive apps)
  3. keep the focus-session timeline current
  4. run the four-rule sampler; if it says capture, grab + OCR + log

The decision core (livingpc.sampler.decide) is pure; this module owns the I/O
and threads SamplerState between ticks.
"""
from __future__ import annotations

import os
import time

from .config import Config
from .storage import EventLog, now_iso
from .sysinfo import get_backend
from .sampler import SamplerState, Decision, decide, ahash, hamming
from .capture.window import WindowTracker
from .capture.screen import ScreenCapturer
from .capture.extras import BrowserHistoryCollector, ClipboardCollector
from .lockfile import InstanceLock
from .diagnostics import log_diag

# Anchor lock/stop files to the PROJECT folder (parent of the livingpc package),
# NOT the current working directory — otherwise processes launched from different
# directories use different lock/stop files, defeating single-instance and stop.
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_PATH = os.path.join(_APP_DIR, "capture.lock")
STOP_PATH = os.path.join(_APP_DIR, ".capture_stop")


def _window_key(app: str, title: str) -> str:
    return f"{app}|{title}"


def run(config: Config, log=print, stop_event=None, on_capture=None,
        single_instance=True, pause_event=None) -> None:
    """Run the capture loop.

    stop_event:  optional threading.Event; when set, the loop exits cleanly.
    on_capture:  optional callback(reason, app, title) fired on each capture.
    single_instance: if True, refuse to start when another capture already holds
                 the lock (prevents double-writing), and exit if a stop file appears.
    """
    lock = None
    if single_instance:
        lock = InstanceLock(LOCK_PATH)
        if not lock.acquire():
            log("[livingpc] capture already running elsewhere — not starting a second.")
            log_diag("service", f"lock denied lock_path={LOCK_PATH}")
            return
        log_diag("service", f"lock acquired lock_path={LOCK_PATH} stop_path={STOP_PATH}")
    else:
        log_diag("service", f"started without single_instance stop_path={STOP_PATH}")
    if os.path.exists(STOP_PATH):
        try:
            os.remove(STOP_PATH)
            log_diag("service", f"cleared stale stop file at startup stop_path={STOP_PATH}")
        except OSError:
            log_diag("service", f"could not clear stale stop file stop_path={STOP_PATH}")

    store = EventLog(config.db_path)
    backend = get_backend()
    tracker = WindowTracker(store)
    capturer = ScreenCapturer(config.blob_dir, ocr_enabled=config.ocr_enabled)
    state = SamplerState()

    clip = ClipboardCollector() if config.clipboard_enabled else None
    browser = BrowserHistoryCollector() if config.browser_history_enabled else None
    last_clip = 0.0
    last_browser = 0.0
    was_paused = False

    log(f"[livingpc] capture started. db={config.db_path} blobs={config.blob_dir}")
    log("[livingpc] Ctrl-C to stop.")
    log_diag(
        "service",
        "capture loop started "
        f"db={os.path.abspath(config.db_path)} blobs={os.path.abspath(config.blob_dir)} "
        f"tick={config.tick} browser={config.browser_history_enabled} "
        f"clipboard={config.clipboard_enabled}",
    )
    try:
        while not (stop_event is not None and stop_event.is_set()):
            tick_start = time.time()
            if os.path.exists(STOP_PATH):     # external stop signal (background daemon)
                try:
                    os.remove(STOP_PATH)
                except OSError:
                    pass
                log("[livingpc] stop signal received — exiting.")
                log_diag("service", f"stop file observed stop_path={STOP_PATH}")
                break
            if pause_event is not None and pause_event.is_set():
                if not was_paused:
                    log_diag("service", "pause_event observed; screen capture loop paused")
                    was_paused = True
                _sleep_remainder(tick_start, config.tick)
                continue
            if was_paused:
                log_diag("service", "pause_event cleared; screen capture loop resumed")
                was_paused = False
            ts = now_iso()
            app, title = backend.foreground()
            idle = backend.idle_seconds()
            window = _window_key(app, title)

            # --- extra collectors (own intervals; clipboard runs even for
            #     blocklisted apps so it can skip+remember, not leak later) ---
            if clip is not None and (tick_start - last_clip) >= config.clipboard_poll_seconds:
                last_clip = tick_start
                try:
                    if clip.poll(store, app, config.blocklist):
                        log_diag("service", "clipboard event logged")
                except Exception:
                    log_diag("service", "clipboard poll failed")
            if browser is not None and (tick_start - last_browser) >= config.browser_poll_seconds:
                last_browser = tick_start
                try:
                    n = browser.poll(store)
                    if n:
                        log_diag("service", f"browser events logged count={n}")
                except Exception:
                    log_diag("service", "browser poll failed")

            # --- blocklist: never capture these apps ----------------------
            if app in config.blocklist:
                state.last_window = window  # avoid a spurious window_change next tick
                log_diag("service", f"blocklisted foreground app skipped app={app}")
                _sleep_remainder(tick_start, config.tick)
                continue

            # --- keep the session timeline current ------------------------
            session_id = tracker.update(app, title, ts)

            # --- compute frame distance only when needed (rule 3) ---------
            frame_distance = None
            image = None
            active_same_window = idle <= config.idle_limit and window == state.last_window
            if active_same_window:
                image = capturer.grab()
                cur_hash = ahash(image)
                frame_distance = (
                    hamming(cur_hash, state.last_hash)
                    if state.last_hash is not None
                    else 999  # force-capture if we've never captured this window
                )

            decision: Decision = decide(
                now=tick_start,
                window=window,
                last_window=state.last_window,
                idle_seconds=idle,
                idle_limit=config.idle_limit,
                frame_distance=frame_distance,
                threshold=config.threshold_for(app),
                seconds_since_capture=tick_start - state.last_capture_ts,
                max_interval=config.max_interval,
            )

            if decision.capture:
                if image is None:
                    image = capturer.grab()
                path = capturer.save(image, ts)
                text = capturer.ocr(image)
                store.log_event(
                    "ocr",
                    app=app,
                    window_title=title,
                    text_payload=text,
                    blob_ref=path,
                    session_id=session_id,
                    ts=ts,
                )
                state.last_hash = ahash(image)
                state.last_capture_ts = tick_start
                log(f"[capture:{decision.reason}] {app} :: {title[:60]}")
                log_diag(
                    "service",
                    f"ocr capture logged reason={decision.reason} app={app} idle={int(idle)}",
                )
                if on_capture is not None:
                    on_capture(decision.reason, app, title)

            state.last_window = window
            _sleep_remainder(tick_start, config.tick)
    except KeyboardInterrupt:
        log("\n[livingpc] stopped.")
        log_diag("service", "keyboard interrupt")
    finally:
        try:
            tracker.close(now_iso())
        except Exception:
            pass
        store.close()
        if lock is not None:
            lock.release()
            log_diag("service", f"lock released lock_path={LOCK_PATH}")


def _sleep_remainder(tick_start: float, tick: float) -> None:
    elapsed = time.time() - tick_start
    remaining = tick - elapsed
    if remaining > 0:
        time.sleep(remaining)
