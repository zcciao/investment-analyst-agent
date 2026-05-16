# Personal Investment AI — Strategic One-Pager

**Stage:** Architecture v3 (4 agents, Validator shipped) · **Mode:** Build for self first → productize if it works

---

## 🎯 The Reframe

> **A research analyst that knows your portfolio, your theses, and your past mistakes — and tells you only what would change your mind.**

Not another information aggregator. Not a robo-advisor. A **thesis tracker with a watcher and a reviewer.** The product never makes decisions for you — it makes you a sharper decider.

---

## 🔍 Focus & Scope

| In scope | Out of scope (for now) |
|---|---|
| Active bets with conviction (your 20%: MSFT, TSLA, GOOG…) | ETF autopilot (the 80%) |
| Loop 2: conviction building | Loop 1: idea discovery (saturated market) |
| Loop 3: position management | Day-trading, options, crypto strategy |

**Why this wedge:** Loop 1 is dominated by YouTubers and news. Loops 2 & 3 are where retail investors actually lose money — and where almost no tool serves them well.

---

## 🧱 The Thesis Object (the spine of the product)

Every position is backed by a structured thesis:

- **One-liner** + direction + conviction (1–10) + time horizon
- **Pillars** = load-bearing claims; each has:
  - Current value (e.g., *"Cloud growth 63% YoY"*)
  - **`threshold_break`** (e.g., *"<40% for 2 quarters"*) ← the magic field
  - `watch_signals` = data sources that move this pillar
  - Status: `intact` / `wobbling` / `broken` / `strengthening`
    - **`broken`** = adverse breach only (metric crossed the threshold in the *wrong* direction)
    - **`strengthening`** = the symmetric upside — metric materially better than threshold *and* improving. Catches under-sized winners that would otherwise stay quietly `intact`.
- **Catalysts** = known future events (earnings, launches)
- **Exit plan** = take-profit · thesis-break · time-stop
- **Source material** = the article/video that seeded the thesis
- **Journal + revisions** = living document

**Killer UX:** paste a YouTube post → AI extracts draft pillars → you accept/edit. No YAML by hand.

---

## 🤖 The Agent Architecture (v3)

**Four agents.** Three on the live loop (Watcher → Analyst → Supervisor) that decide *what's happening now*, plus a retrospective **Validator** the user triggers per thesis to grade *whether the thesis is still well-calibrated*. The Validator is on a parallel branch, not in the live loop — it reads from Thesis store + historical data, outputs a report + proposed changes, and lets the user decide what to apply.

```
                                            ┌──────────┐
  Markets                                   │  Thesis  │ ──► Trigger/Push ──► You
  Social ─► Watcher ─► Updates ─► Info  ┐   └────▲─────┘                       │
  News                                Storage    │                              ▼
  Fundamentals                          │     approve                     "Validate"
                                        ▼        │                              │
                                     Analyst ─► Thesis Updates                  ▼
                                        ▲              │                  ┌──────────┐
                                        │              ▼                  │Validator │
                                     Feedback ◄── Supervisor              └────┬─────┘
                                                                               │
                                                                  history + current data
                                                                  → verdicts + proposed
                                                                    changes
                                                                               │
                                                                       You review/apply
                                                                               │
                                                                               ▼
                                                                          Thesis (updated)
```

| Agent | Clock | Role | Cost |
|---|---|---|---|
| 🔭 **Watcher** | Continuous (cron) | Forward-looking pillar status check on current data + recent news. Emits one of four statuses per pillar (`intact / wobbling / broken / strengthening`). Pre-filters before invoking the LLM. | Cheap, small model |
| 🔬 **Analyst** | On-demand (chat) | Conversational research — pull live data, explain thesis state, flag risks, and *explicitly* prompt the user to revise on broken or strengthening pillars. | Strong model, rare |
| 🛡️ **Supervisor** | Per Analyst output | Quality-gate the proposed update before it touches the Thesis store. **Approve** → commit + Trigger/Push. **Reject** → Feedback edge, Analyst retries with the critique. | Strong model, scoped |
| 🩺 **Validator** | On-demand (per thesis) | Backward-looking grade. Pulls price history + fundamentals over the holding window, returns a narrative + per-pillar verdicts (`held / missed / too_loose / too_tight / untested`) + a structured `proposed_changes` payload (refined thresholds, conviction, exit plan) + a recommendation (`hold / exit / add / watch / revise`). Writes nothing directly — the user reviews and applies. | Strong model, rare |

**Watcher vs. Validator — orthogonal, not redundant:**

| | Watcher | Validator |
|---|---|---|
| Direction | Forward — *is it triggered now?* | Backward — *did it hold up?* |
| Clock | Continuous (cron) | On-demand (you click) |
| Granularity | Per-pillar status (4 levels) | Per-pillar verdict + recalibrated threshold |
| Output | Status change alerts | Narrative + verdicts + proposed thesis changes |
| Your role | Read alert, maybe revise | Review report, apply or discard |

**Two state stores are first-class, not implementation details:**

- **Info Storage** — durable buffer of Watcher updates. Lets Analyst run on its own clock, lets you replay an analysis, and is the natural place to throttle cost.
- **Thesis** — the source of truth for every position. Only the Supervisor (via the live loop) or the user (via Validator-approved updates) can write to it. Every committed change ships through **Trigger / Push** to you — you're notified, not queried.

**Why this is better than v1:**
- **Push, not pull.** You don't open the app to find out something's wrong; it tells you. The Trigger/Push step is the product surface, not the chat box.
- **Feedback loop = self-correction.** Hallucinated numbers and weak reasoning get caught by the Supervisor before they touch your thesis store. Cheaper than baking it all into one prompt.
- **Maps cleanly to LangGraph.** Each agent is a node; Info Storage and Thesis are checkpointer-backed state; the Supervisor → Feedback → Analyst arc is a conditional edge. The Validator is its own small graph (`gather_node → analyze_node → done_node`).

**Where Challenger and Coach go:**
- **Coach** got built — as the **Validator**, the interactive form. The originally-planned scheduled weekly batch felt worse than letting the user pull a back-check when they're already thinking about a thesis. Scheduled Coach is now optional future work.
- **Challenger** remains a future Supervisor mode (or sibling reviewer) triggered by *your* behavior, not the Watcher. *"You're adding to GOOG. 2 pillars wobbling. Want the bear case?"*

This keeps the core loop small while giving the user a real retrospective surface, on demand.

---

## 📚 vs. TradingAgents (the OSS benchmark)

| | TradingAgents | Your product |
|---|---|---|
| State | Stateless | Stateful (theses + behavior + Info Storage) |
| Output | BUY/SELL/HOLD | Perspective + alerts |
| Trigger | Pull (you invoke) | **Push (Trigger/Push to user)** |
| Personalization | None | Core moat |
| Best for | Research / backtesting | Living portfolio |

**Borrow:** bull/bear debate, role decomposition, LangGraph orchestration
**Don't copy:** stateless runs, decision-maker framing
**Combo strategy:** use TradingAgents as the Analyst's deep-research engine when an Update warrants it; the rest of the pipeline is yours.

> *TradingAgents is a Bloomberg Terminal. You're building a personal CFO.*

---

## ⚠️ Landmines (decide early)

1. **Hallucinated numbers = real money lost.** Cite every figure; Supervisor must reject any pillar change without a sourced value.
2. **Cost runaway.** Watcher must be near-free. Pre-filter *before* the LLM call, not in it. Info Storage exists partly so you can rate-limit Analyst runs.
3. **Supervisor loop that never converges.** Cap Analyst retries (e.g., 2 rounds of Feedback), and require Supervisor critiques to be *specific* — otherwise it's just expensive disagreement.
4. **Push fatigue.** Trigger/Push must fire only on material changes. The bar for interrupting you is higher than the bar for committing to the Thesis store.
5. **Latency theater.** Most queries <5s. Save the full Watcher→Analyst→Supervisor pipeline for things that actually move a pillar.
6. **Privacy.** Portfolio + thesis + behavior = sensitive. Plan for it before productizing.

---

## 🪜 Build Sequence

**Phase 1 — Skeleton you can talk to (✅ done)**
1. ✅ Thesis object + paste-to-pillars import flow (LangGraph builder: draft → research → refine → done)
2. ✅ Analyst node wired to yfinance + thesis store; pillar-aware prompts that nudge revisions on broken/strengthening
3. ✅ Conversational UI for "what's up with GOOG?" with SSE streaming
4. **Validation gate:** *Am I opening it weekly without forcing myself?*

**Phase 2 — Close the loop (the v3 diagram)**
5. ✅ **Watcher** as a scheduled cron + on-demand: per-thesis `watch_signals` → status check → Alerts + Runs log
6. ✅ **Validator** (the Coach role, reimagined): per-thesis back-check that grades pillars against history and proposes structured updates the user can review and apply
7. ☐ **Supervisor** node + Feedback edge: Analyst proposes thesis updates, Supervisor approves/rejects/critiques (still TBD — today the user is effectively the supervisor for Validator-proposed changes)
8. ☐ **Trigger / Push** channel (start with email or a single push surface) on Supervisor-approved updates only
9. **Validation gate:** *Did the last 5 pushes actually deserve to interrupt me?*

**Phase 3 — Layered roles & productize**
10. ☐ Challenger mode on Supervisor, triggered by your behavior (adding to a wobbling position)
11. ☐ Scheduled Coach (optional) — a weekly batch over Thesis + action history. May not be needed now that on-demand Validator exists; keep open until the daily flow tells us.
12. ☐ Multi-user, broker integrations, pricing, compliance

---

## ❓ Open Questions

1. ~~Pillar status: binary / ternary / 0–100 score?~~ **Resolved: four-status enum** — `intact / wobbling / broken / strengthening`. Asymmetric on purpose: `broken` = adverse breach; `strengthening` = symmetric upside that catches under-sized winners. Watcher and Validator both use these.
2. **One thesis per ticker, or many** (long-term + tactical)?
3. Supervisor approves writes to the Thesis store — but does it also auto-update pillar `current_value`, or always ask the Analyst (and you) for sign-off?
4. **Info Storage retention:** rolling 30 days? Per-thesis quota? Never delete (for replay)?
5. **Trigger/Push surface:** email, native push, a single "morning digest," or all three gated by severity?
6. First non-news signal beyond price: **SEC filings · insider flow · earnings transcripts?**
7. **Validator cost ceiling.** Each back-check is one strong-model call against a long context (history + financials + alerts + journal). Cheap for personal use; could matter at scale. Cache or rate-limit?

---

## 🎁 The Unfair Angle

Bloomberg, Seeking Alpha, and fintech apps **structurally cannot personalize to your portfolio + thesis + behavior** — they serve millions. A small AI-native tool that knows *you*, watches on your behalf, and only interrupts when a pillar actually moves is a moat they can't cross.

---

## ▶️ Next Session Decision Points

- [ ] Decide Supervisor's contract: what fields must a Thesis Update carry for it to be approvable? (Validator already produces a clean `proposed_changes` schema — borrow from it.)
- [ ] Sketch Watcher + Validator unit economics — per-day cost on 10 holdings × 4 input domains, plus on-demand Validator runs
- [ ] Pick the first Trigger/Push surface (and the severity threshold that fires it)
- [ ] Resolve the remaining open questions above (Q1 done)
- [ ] Build Supervisor + Feedback loop to close the live loop properly (Watcher's status changes currently land directly without LLM-side approval)
