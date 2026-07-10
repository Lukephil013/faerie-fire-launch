# Faerie Fire — Design Document

> Historical design intent. For current behavior use `FEATURES.md`; for agent
> routing and invariants use `AGENTS.md`. See `docs/INDEX.md`.

*A local-first personal context engine: an ambient capture pipeline that feeds a human-curated, temporally-aware second brain, plus a screen-aware assistant that uses it. (Project formerly "The Living Computer.")*

Version 0.1 — design draft · Platform: Windows + Python

---

## 1. What this is

The system has two halves that are deliberately built in sequence:

**Half 1 — the context engine (the second brain).** It quietly records what you do on your PC, then once a day distills that raw activity into a small set of plain-English statements. You approve, edit, or reject each one. Approved statements become durable memory, organized by category and tracked *over time* so the system understands not just what is true now, but how things have changed. This is the foundation, and it gets built first.

**Half 2 — the screen-aware assistant.** Once the memory layer is stable, a live component watches the current screen and answers questions using both what's on screen *right now* and the accumulated memory. This is downstream: it is mostly a *consumer* of the memory built in Half 1, which is why it comes second.

The guiding principle throughout is **local-first and human-in-the-loop**. Raw data stays on your machine. You are the approval gate for what becomes memory, which keeps the second brain clean and keeps you in control of what it believes about you.

---

## 2. The core idea, named

Three concepts make the rest of the design coherent.

**Episodic vs. semantic memory.** The raw activity log (what window was focused, what text was on screen, what you browsed) is *episodic* — a firehose of timestamped events. The approved statements ("plays crit-carry ADCs in League", "studying Korean using Anki + TTMIK") are *semantic* — distilled, durable facts. The triage step is the bridge between the two, and it's the heart of the system.

**Temporal knowledge graph.** Memory is not a flat list of current facts. Each fact carries a validity window and can be *superseded* rather than overwritten. When you switch from playing Jinx and Jhin to Caitlyn and Tristana, the old fact isn't deleted — it's closed out ("valid March–May") and the new one is linked to it. This preserves your *trajectory*, which is the actual value of a second brain. A flat store would throw that away.

**Human-in-the-loop consolidation.** Nothing enters semantic memory without your approval. The system proposes; you dispose. This is what makes a long-lived memory store trustworthy instead of slowly accumulating garbage.

---

## 3. Architecture at a glance

```
                    ┌─────────────────────────────────────────┐
                    │              CAPTURE LAYER                │
                    │  active-window · screenshots+OCR ·        │
                    │  browser history · clipboard              │
                    └───────────────────┬─────────────────────┘
                                        │ events
                                        ▼
                    ┌─────────────────────────────────────────┐
                    │           RAW EVENT LOG (SQLite)          │
                    │  lightweight rows kept long-term;         │
                    │  screenshots in rolling buffer, purged    │
                    │  after each daily triage                  │
                    └───────────────────┬─────────────────────┘
                                        │ daily aggregate
                                        ▼
                    ┌─────────────────────────────────────────┐
                    │            TRIAGE PIPELINE                │
                    │  summarize per category → LLM proposes    │
                    │  new statements + supersessions +         │
                    │  clarifying questions                     │
                    └───────────────────┬─────────────────────┘
                                        │ candidates
                                        ▼
                    ┌─────────────────────────────────────────┐
                    │          DAILY DIGEST (review UI)         │
                    │     you approve / edit / reject           │
                    └───────────────────┬─────────────────────┘
                                        │ approved
                                        ▼
                    ┌─────────────────────────────────────────┐
                    │         MEMORY GRAPH (SQLite)             │
                    │   temporal facts with supersession        │
                    └───────────────────┬─────────────────────┘
                                        │ queried by
                                        ▼
                    ┌─────────────────────────────────────────┐
                    │   PHASE 2: SCREEN-AWARE ASSISTANT (Q&A)   │
                    │   live frame + relevant memory → answer   │
                    └─────────────────────────────────────────┘
```

---

## 4. Capture layer

Four collectors run continuously in the background and write to a single raw event log. (Keystroke logging is intentionally **excluded from v1** — see §8 — but the architecture leaves room to add it later.)

**Active window + app titles.** Polls the foreground window title every ~2 seconds via `pywin32`. This is the lightweight backbone of the whole system: it segments your day into *focus sessions* ("League client 8:00–8:45", "Anki 9:00–9:20", "Chrome — grammar article 9:20–9:35"). Everything else hangs off these sessions, and on its own it's already surprisingly high-signal. Kept long-term.

**Periodic screenshots + on-device OCR.** Captures a frame on an interval (and/or when the foreground window changes) using `mss`, then immediately runs **on-device OCR** (RapidOCR or Tesseract) to extract the visible text. The *text* is stored; the *image* lives only in a rolling buffer and is deleted after each daily triage. This is the layer that actually "sees what you're doing" — which champion is on screen, which vocab card is up — without keeping heavy image data around.

**Browser history + clipboard.** Reads the browser history SQLite databases (Chrome/Edge/Firefox store these locally) on an interval, and polls the clipboard for copied text. Both are light and high-value for study/research context. Kept long-term.

### 4.1 Screenshot sampling logic

Don't capture on a naïve fixed timer — you'd drown in near-identical frames. The capture loop wakes on a short tick (~2s, the same tick the active-window poller uses, so it's nearly free), but *waking* isn't *capturing*. On each tick it evaluates four rules in priority order and only fires a screenshot if one says yes:

1. **Are you active at all?** If there's been no mouse/keyboard input for ~60s (via `GetLastInputInfo`), you're AFK — skip. No value in capturing a screen you walked away from, and this kills a large fraction of useless capture on its own.
2. **Did the foreground window just change?** If the front app or window title differs from the last tick, capture immediately. Context switches are the highest-signal moments (you just jumped from the game to a champion guide), and this rule is event-driven, not timer-driven — so important transitions are caught regardless of how lazy the heartbeat is set.
3. **Has the screen changed enough?** Still in the same window? Compare the current frame to the last captured one with a cheap perceptual hash (downscale to ~8×8, hash, compare). Near-identical (reading a static page, idling in champ select) → skip. Meaningfully different (new Anki card, game state moved) → capture. This is what prevents hundreds of duplicate frames of one screen.
4. **Heartbeat.** A safety net: even if nothing above tripped, capture once every `MAX_INTERVAL` (30–60s) while active, so long stable sessions still get some coverage and slow changes aren't missed.

```python
def should_capture(now, state):
    if idle_seconds() > 60:
        return False                                   # rule 1: AFK
    if foreground_window() != state.last_window:
        return True                                    # rule 2: context switch
    if frame_diff(current, state.last_frame) > THRESHOLD:
        return True                                    # rule 3: screen changed enough
    if now - state.last_capture > MAX_INTERVAL:
        return True                                    # rule 4: heartbeat
    return False
```

**The two knobs.** `THRESHOLD` (how different counts as "different") and `MAX_INTERVAL` (the heartbeat). Loosen both for less storage and lower triage cost; tighten for more fidelity. Because rule 2 is event-driven, you can run quite sparse and still never miss a context switch.

**Per-app thresholds.** `THRESHOLD` can vary by app: a game wants dense sampling (state changes fast and matters), a text document wants sparse (mostly static). Since the active-window collector already knows the foreground app, it can hand the right threshold to the sampler automatically.

---

## 5. Storage

Two local SQLite databases, both encrypted at rest (see §8).

### 5.1 Raw event log

A single append-only table of events. Heavy payloads (screenshots) are referenced, not inlined, so they can be purged independently.

```
event(
  id            INTEGER PRIMARY KEY,
  ts            DATETIME,         -- when it happened
  type          TEXT,             -- 'window' | 'ocr' | 'browser' | 'clipboard'
  app           TEXT,             -- e.g. 'LeagueClient.exe'
  window_title  TEXT,
  text_payload  TEXT,             -- OCR text, URL, clipboard contents (nullable)
  blob_ref      TEXT,             -- path to screenshot in rolling buffer (nullable)
  session_id    INTEGER           -- FK to focus session, for grouping
)
```

**Retention (your choice — "keep metadata, discard heavy data"):** lightweight rows (`window`, `browser`, `clipboard`, and the OCR `text_payload`) are kept long-term. Screenshot blobs live in a rolling buffer and are deleted right after the daily triage consumes them. So you can always look back at *what* you were doing in text form; you just won't have the original pixels.

### 5.2 Memory graph

The second brain. Each row is a single temporal fact.

```
memory(
  id            INTEGER PRIMARY KEY,
  subject       TEXT,        -- usually 'user' (room to grow to other entities)
  category      TEXT,        -- 'League of Legends', 'Korean study', 'work', ...
  attribute     TEXT,        -- 'champion pool', 'study resources', 'sleep schedule'
  value         TEXT,        -- 'Caitlyn, Tristana (crit ADCs)'
  valid_from    DATE,
  valid_to      DATE,        -- NULL = currently true
  status        TEXT,        -- 'active' | 'superseded' | 'rejected'
  supersedes_id INTEGER,     -- FK to the memory this one replaced (nullable)
  confidence    REAL,        -- model/user confidence 0–1
  source_refs   TEXT,        -- JSON list of event ids this was distilled from
  approved_at   DATETIME
)

category(
  name          TEXT PRIMARY KEY,
  description   TEXT,
  created_at    DATETIME
)
```

The `valid_to` + `supersedes_id` columns are what turn a flat fact store into a temporal graph — see the worked example in §7.

---

## 6. Triage pipeline (the daily digest)

Runs once a day (your choice — daily cadence keeps the backlog small and builds a habit loop). The flow:

1. **Aggregate.** Pull the day's events, group them by focus session and then by inferred category. Build a compact per-category summary (e.g. "League: 3 sessions, ~2h, champions seen on screen: Caitlyn, Tristana; bought IE, Collector").

2. **Recall context.** For each touched category, load the currently-*active* memories. This is critical — the model must see what it already believes in order to detect change rather than blindly re-asserting.

3. **Propose.** Send the LLM *both* the day's summary *and* the active memories for those categories. It returns three things:
   - **New statements** — facts not yet in memory.
   - **Supersession proposals** — "today's activity conflicts with / extends memory #47; suggest closing #47 and adding this."
   - **Clarifying questions** — where it's unsure ("You had a Korean grammar PDF and a drama on screen — studying, or watching for fun?").

4. **Review.** The candidates surface in a daily digest UI. For each, you **approve / edit / reject**. Approvals write to the memory graph; supersessions close out the old fact (`valid_to` = today, `status` = superseded) and link the new one via `supersedes_id`.

5. **Purge.** Once triage is committed, delete the day's screenshot blobs from the rolling buffer.

**Why the conflict-detection step is the whole game.** Anyone can append facts. The hard, valuable part is recognizing "this isn't new, it's a *change* to something I already know" and threading the supersession link correctly. That single capability is the difference between a second brain that tells you *"you've shifted from enchanters to crit ADCs over the last two months"* and one that just piles up disconnected notes. It's also the reason for the LLM recommendation in §9.

**Digest UI for v1.** Keep it dead simple: a local single-page web app (served from the Python process) or a terminal UI listing each candidate card with approve/edit/reject buttons. No need for anything fancy until the loop feels good.

---

## 7. Worked example: the Jinx → Caitlyn transition

This is the scenario you described, traced through the schema.

**Week 1.** OCR + window logs show heavy League play with Jinx and Jhin on screen. Daily triage accumulates evidence; at week's end it proposes:

> *"Has been playing Jinx and Jhin in League of Legends."*

You approve. A row is written:

```
id=47  category='League of Legends'  attribute='champion pool'
value='Jinx, Jhin'  valid_from=2026-03-02  valid_to=NULL  status='active'
```

**One month later.** Logs now show Caitlyn and Tristana. Triage loads active memory #47, notices the new champions don't match, and proposes a **supersession** plus a clarifying question:

> *"Champion pool looks like it changed to Caitlyn and Tristana — replace the earlier Jinx/Jhin note? (Both are crit-scaling ADCs.)"*

You approve. Two writes happen:

```
id=47  ...  valid_to=2026-04-05  status='superseded'      (closed out)
id=88  category='League of Legends' attribute='champion pool'
value='Caitlyn, Tristana'  valid_from=2026-04-05  valid_to=NULL
status='active'  supersedes_id=47
```

Now the graph knows both the current state *and* the history. A later question like "have my champion preferences changed recently?" can walk the `supersedes_id` chain and answer with the trajectory, not just the latest value.

---

## 8. Privacy & security model

This system records a lot about you, so the security model is a first-class design concern, not an afterthought.

**Everything is local by default.** Capture, storage, and the digest UI all run on your machine. The only thing that ever leaves (and only if you choose the cloud LLM in §9) is the *distilled daily summary text* — never raw screenshots, never the full event log.

**Encryption at rest.** Both SQLite databases are encrypted (SQLCipher, or an encrypted volume). If the disk or a backup is ever exposed, the contents aren't readable.

**Application blocklist.** A configurable list of apps whose activity is never captured at all — password managers, banking apps, anything you designate. When a blocklisted app is foreground, the capture layer pauses.

**Redaction before any cloud call.** Before the daily summary is sent to a cloud LLM, a redaction pass scrubs obvious sensitive patterns (card-number-shaped digit runs, emails, things that look like credentials). If you later choose a fully-local model, this becomes moot but harmless.

**Keystrokes: excluded from v1.** Originally considered, now deliberately left out for the first version. Rationale: keystrokes are the lowest-signal-per-byte and highest-risk source (they capture passwords, 2FA codes, private messages), and the active-window + OCR layer already gives the triage model most of what keystrokes would. The schema and capture interface are built so a redacted, blocklist-aware keystroke collector *could* be added later if a concrete gap appears — but it's off by default and out of scope for now.

---

## 9. LLM backend decision

**Recommendation: a pluggable LLM interface, defaulting to a cloud model (Claude) during the build, with a local model (via Ollama) as a drop-in alternative.**

The reasoning:

- The make-or-break task is the **conflict-detection / supersession reasoning** in triage. Getting it wrong slowly corrupts the second brain. That nuanced "extend vs. contradict vs. replace" judgment is exactly where a strong cloud model clearly outperforms a local 7–13B model today.
- The privacy cost is smaller than it first appears: only the *distilled, redacted daily summary text* is sent — not screenshots, not raw logs.
- So: use the cloud model to get the memory layer *trustworthy* first. Because every LLM call goes through one interface, switching to a fully-local model later is a config change, not a rewrite. You keep quality now and a clean path to fully-local whenever you want it.

```
class LLMBackend(Protocol):
    def triage(self, day_summary: str, active_memories: list[Memory]) -> TriageResult: ...

# v1 default:  ClaudeBackend()   — best distillation quality
# drop-in:     OllamaBackend()   — fully local, nothing leaves the machine
```

Keystroke-derived text would be local-only regardless — but since keystrokes are excluded from v1, this is a non-issue for now.

---

## 10. Phase 2: the screen-aware assistant

Built only after the memory layer is stable, because it depends on the schema.

When invoked (a global hotkey is the least intrusive trigger, especially mid-game), it: captures the current frame, OCRs it, infers the active category, pulls the relevant active memories for that category, and answers using both. Examples:

- *In League:* "What should I build into this comp?" → it sees the enemy champions on screen and knows your champion pool and playstyle from memory.
- *Studying Korean:* it surfaces a vocab or grammar tidbit tied to what's currently on screen, and can quiz you on items it has seen you study before.

The interaction surface (overlay vs. side window vs. voice) is a Phase 2 decision and doesn't need to be settled now.

---

## 11. Build roadmap

A suggested order that always leaves you with something runnable.

1. **Capture skeleton.** Active-window poller + the raw event log (SQLite) + focus-session grouping. Run it for a few days and just look at the data. *Deliverable: you can see a clean timeline of your computer use.*
2. **Add OCR + screenshots.** The `mss` capturer, on-device OCR, the rolling buffer + purge logic, plus browser-history and clipboard collectors. *Deliverable: rich text-level record of activity.*
3. **Memory graph + manual entry.** Create the memory schema and a tiny UI to add/supersede facts by hand. Proves the temporal model works before any AI is involved.
4. **Triage pipeline.** The daily aggregate → LLM → candidate statements loop, behind the pluggable LLM interface, with the conflict-detection prompt. *Deliverable: the daily digest you approve.*
5. **Harden privacy.** Encryption, app blocklist, redaction pass. *Deliverable: safe to run continuously.*
6. **Phase 2 — screen-aware assistant.** Hotkey, live frame + memory query, answers. *Deliverable: the "ask it anything about what I'm doing" experience.*

---

## 12. Tech stack summary

| Concern | Choice |
|---|---|
| Language / runtime | Python 3.11+, Windows |
| Active-window capture | `pywin32` |
| Screenshots | `mss` |
| On-device OCR | RapidOCR (or Tesseract via `pytesseract`) |
| Browser history | read browser SQLite history DBs directly |
| Clipboard | `pyperclip` / win32 clipboard API |
| Storage | SQLite + SQLCipher (encrypted) |
| Background scheduling | a long-running service loop + an internal daily timer |
| Triage LLM | pluggable: Claude API (default) / Ollama (local) |
| Digest UI | local single-page web app or terminal UI |

---

## 13. Open questions to settle before/while building

- **Category taxonomy.** Fixed starter categories, or let the triage model propose new categories (also subject to your approval)? Recommend the latter, gated by approval.
- **Granularity of facts.** How fine-grained should an attribute be ("champion pool" vs. "favorite champion" vs. "win rate by champion")? Start coarse; split later.
- **Digest fatigue.** What's the max number of candidate cards per day before it feels like a chore? Tune the triage prompt to surface only the top-N most significant changes.
- **Idle / AFK handling.** Detect idle time so the timeline doesn't log you "using" a window you walked away from.
- **Multi-monitor.** Capture all displays or only the foreground one? (Foreground-only is cheaper and usually enough.)

---

*End of draft. The natural next artifact after this is either (a) the capture-layer scaffold from roadmap step 1, or (b) the memory-graph schema + a s
---

# Phase 2 — The Companion (blueprint)

The companion is the Faerie Fire brain given a **face, a voice, ears, and a
personality**. It is the original vision: an ethereal presence (Cortana-like)
that hangs out on your desktop, knows you from the memory graph, can see your
screen, talks back, and — with permission — chimes in unprompted.

## Components
1. **Face window** — a frameless, always-on-top, transparent window rendering an
   audio-reactive ethereal visual (glowing particles + luminous eyes) with states:
   idle, listening, thinking, speaking. Rendered with web tech in a WebView2
   window driven from Python (pywebview). Free to run.
2. **Ears (STT)** — local Whisper (GPU), triggered by a wake word ("Hey Faerie")
   *and* a push-to-talk hotkey, with voice-activity detection.
3. **Voice (TTS)** — local Piper to start, behind a swap-in interface so a premium
   voice (e.g. ElevenLabs) can replace it later.
4. **Brain** — Claude, given the active persona + relevant memories + recent
   screen context + conversation history. This fuses the screen-aware assistant
   with voice and personality.
5. **Personas / modes** — each persona is a named bundle of {system prompt,
   proactivity level, voice tone, accent color}. Built-ins: *Companion* (default),
   *Coach* (LoL, tactical), *Gremlin* (lightly flaming, for funny videos).
   Hot-swappable now; auto-selected by detected activity later. Stored as editable
   config so new personas can be invented.
6. **Proactive engine** — unprompted commentary ("freedom of expression"). The
   risky part (cost + annoyance), tamed by: routing ambient banter to a cheap/fast
   model (Haiku) while substantive answers use a stronger one; per-persona
   proactivity dials; cooldowns + an hourly cap; instant "shush"/mute.

## Cost model
Local face + ears + voice + wake word = $0 recurring. Only the conversation costs
(Claude, pay-per-turn). Tiering (Haiku for banter, Sonnet for help) keeps even a
chatty proactive companion in the low single digits $/month, capped by the spend
limit. If it ever grows pricey, the fallback is a fully-local model.

## Build order (each step usable on its own)
- **A — Persona brain (text):** personas + memory + screen context as a text chat.
- **B — Ethereal face window:** the visual shell wired to A (talks in text bubbles,
  reactive idle/thinking/speaking states).
- **C — It speaks:** TTS; face pulses to its voice.
- **D — You speak:** wake word + push-to-talk + Whisper.
- **E — Proactive engine:** unprompted commentary with cost/cooldown controls.
- **F — Auto-persona switching** by detected activity.

First milestone: **A + B fused** — an ethereal face on the desktop that already has
personality, knows you from memory, sees your screen, and talks back in text.
Voice bolts on after.

## Python <-> face contract (A+B)
- JS -> Python: `api.send(text)` (user message), `api.set_persona(name)`,
  `api.list_personas()`.
- Python -> JS: `faceState(state, color)` and `addMessage(role, text)` via
  `window.evaluate_js(...)`. The brain runs in a worker thread so the UI never
  blocks; it sets `thinking` on send and `speaking` while delivering the reply.
