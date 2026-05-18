# Personal Investment AI — Strategic One-Pager

**Stage:** Architecture v3 (4 agents · Reviewer shipped · plain-English vocabulary) · **Mode:** Build for self first → productize if it works

> **Vocabulary note.** This doc uses the renamed terms: *plan / reason / walk-away signal / confidence / event / on_track / at_risk / off_track / ahead / Reviewer*. See [`GLOSSARY.md`](./GLOSSARY.md) for the full old→new map (thesis → plan, pillar → reason, etc.).

---

## 🎯 The Reframe

> **A research analyst that knows your portfolio, your plans, and your past mistakes — and tells you only what would change your mind.**

Not another information aggregator. Not a robo-advisor. A **plan tracker with a watcher and a reviewer.** The product never makes decisions for you — it makes you a sharper decider.

---

## 🔍 Focus & Scope

| In scope | Out of scope (for now) |
|---|---|
| Active bets you're confident about (your 20%: MSFT, TSLA, GOOG…) | ETF autopilot (the 80%) |
| Loop 2: confidence building | Loop 1: idea discovery (saturated market) |
| Loop 3: position management | Day-trading, options, crypto strategy |

**Why this wedge:** Loop 1 is dominated by YouTubers and news. Loops 2 & 3 are where retail investors actually lose money — and where almost no tool serves them well.

---

## 🧱 The Plan Object (the spine of the product)

Every position is backed by a structured plan:

- **One-liner** + direction + confidence (1–10) + time horizon
- **Reasons** = load-bearing claims; each has:
  - Current value (e.g., *"Cloud growth 63% YoY"*)
  - **`walk_away_signal`** (e.g., *"<40% for 2 quarters"*) ← the magic field
  - `data_sources` = sources that move this reason (filings, calls, indices)
  - Status: `on_track` / `at_risk` / `off_track` / `ahead`
    - **`off_track`** = adverse breach only (metric crossed the walk-away signal in the *wrong* direction)
    - **`ahead`** = the symmetric upside — metric materially better than the signal *and* improving. Catches under-sized winners that would otherwise stay quietly `on_track`.
- **Events** = known future moves (earnings, launches)
- **Exit triggers** = take-profit · plan-break · time-stop
- **Source material** = the article/video that seeded the plan
- **Journal + revisions** = living document

**Killer UX:** paste a YouTube post → AI extracts draft reasons → you accept/edit. No YAML by hand.

---

## 🤖 The Agent Architecture (v3)

**Four agents.** Three on the live loop (Watcher → Analyst → Supervisor) that decide *what's happening now*, plus a retrospective **Reviewer** the user triggers per plan to grade *whether the plan is still well-calibrated*. The Reviewer is on a parallel branch, not in the live loop — it reads from the Plan store + historical data, outputs a report + proposed changes, and lets the user decide what to apply.

```
                                            ┌──────────┐
  Markets                                   │   Plan   │ ──► Trigger/Push ──► You
  Social ─► Watcher ─► Updates ─► Info  ┐   └────▲─────┘                       │
  News                                Storage    │                              ▼
  Fundamentals                          │     approve                       "Review"
                                        ▼        │                              │
                                     Analyst ─► Plan Updates                    ▼
                                        ▲              │                  ┌──────────┐
                                        │              ▼                  │ Reviewer │
                                     Feedback ◄── Supervisor              └────┬─────┘
                                                                               │
                                                                  history + current data
                                                                  → verdicts + proposed
                                                                    changes
                                                                               │
                                                                       You review/apply
                                                                               │
                                                                               ▼
                                                                          Plan (updated)
```

| Agent | Clock | Role | Cost |
|---|---|---|---|
| 🔭 **Watcher** | Continuous (cron) | Forward-looking reason status check on current data + recent news. Emits one of four statuses per reason (`on_track / at_risk / off_track / ahead`). Pre-filters before invoking the LLM. | Cheap, small model |
| 🔬 **Analyst** | On-demand (chat) | Conversational research — pull live data, explain plan state, flag risks, and *explicitly* prompt the user to revise when a reason is off-track or ahead. | Strong model, rare |
| 🛡️ **Supervisor** | Per Analyst output | Quality-gate the proposed update before it touches the Plan store. **Approve** → commit + Trigger/Push. **Reject** → Feedback edge, Analyst retries with the critique. | Strong model, scoped |
| 🩺 **Reviewer** | On-demand (per plan) | Backward-looking grade. Pulls price history + fundamentals over the holding window, returns a narrative + per-reason verdicts (`held_up / failed / set_too_low / set_too_high / not_enough_data`) + a structured `proposed_changes` payload (refined walk-away signals, confidence, exit triggers) + a recommendation (`hold / exit / add / watch / revise`). Writes nothing directly — the user reviews and applies. | Strong model, rare |

**Watcher vs. Reviewer — orthogonal, not redundant:**

| | Watcher | Reviewer |
|---|---|---|
| Direction | Forward — *is it triggered now?* | Backward — *did it hold up?* |
| Clock | Continuous (cron) | On-demand (you click) |
| Granularity | Per-reason status (4 levels) | Per-reason verdict + recalibrated signal |
| Output | Status change alerts | Narrative + verdicts + proposed plan changes |
| Your role | Read alert, maybe revise | Review report, apply or discard |

**Two state stores are first-class, not implementation details:**

- **Info Storage** — durable buffer of Watcher updates. Lets Analyst run on its own clock, lets you replay an analysis, and is the natural place to throttle cost.
- **Plan store** — the source of truth for every position. Only the Supervisor (via the live loop) or the user (via Reviewer-approved updates) can write to it. Every committed change ships through **Trigger / Push** to you — you're notified, not queried.

**Why this is better than v1:**
- **Push, not pull.** You don't open the app to find out something's wrong; it tells you. The Trigger/Push step is the product surface, not the chat box.
- **Feedback loop = self-correction.** Hallucinated numbers and weak reasoning get caught by the Supervisor before they touch your Plan store. Cheaper than baking it all into one prompt.
- **Maps cleanly to LangGraph.** Each agent is a node; Info Storage and the Plan store are checkpointer-backed state; the Supervisor → Feedback → Analyst arc is a conditional edge. The Reviewer is its own small graph (`gather_node → analyze_node → done_node`).

**Where Challenger and Coach go:**
- **Coach** got built — as the **Reviewer**, the interactive form. The originally-planned scheduled weekly batch felt worse than letting the user pull a back-check when they're already thinking about a plan. Scheduled Coach is now optional future work.
- **Challenger** remains a future Supervisor mode (or sibling reviewer) triggered by *your* behavior, not the Watcher. *"You're adding to GOOG. 2 reasons at risk. Want the bear case?"*

This keeps the core loop small while giving the user a real retrospective surface, on demand.

---

## 📚 vs. TradingAgents (the OSS benchmark)

| | TradingAgents | Your product |
|---|---|---|
| State | Stateless | Stateful (plans + behavior + Info Storage) |
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

1. **Hallucinated numbers = real money lost.** Cite every figure; Supervisor must reject any reason change without a sourced value.
2. **Cost runaway.** Watcher must be near-free. Pre-filter *before* the LLM call, not in it. Info Storage exists partly so you can rate-limit Analyst runs.
3. **Supervisor loop that never converges.** Cap Analyst retries (e.g., 2 rounds of Feedback), and require Supervisor critiques to be *specific* — otherwise it's just expensive disagreement.
4. **Push fatigue.** Trigger/Push must fire only on material changes. The bar for interrupting you is higher than the bar for committing to the Plan store.
5. **Latency theater.** Most queries <5s. Save the full Watcher→Analyst→Supervisor pipeline for things that actually move a reason.
6. **Privacy.** Portfolio + plans + behavior = sensitive. Plan for it before productizing.

---

## 🪜 Build Sequence

**Phase 1 — Skeleton you can talk to (✅ done)**
1. ✅ Plan object + paste-to-reasons import flow (LangGraph builder: draft → research → refine → done)
2. ✅ Analyst node wired to yfinance + plan store; reason-aware prompts that nudge revisions on off-track / ahead
3. ✅ Conversational UI for "what's up with GOOG?" with SSE streaming
4. **Validation gate:** *Am I opening it weekly without forcing myself?*

**Phase 2 — Close the loop (the v3 diagram)**
5. ✅ **Watcher** as a scheduled cron + on-demand: per-plan `data_sources` → status check → Alerts + Runs log
6. ✅ **Reviewer** (the Coach role, reimagined): per-plan back-check that grades reasons against history and proposes structured updates the user can review and apply
7. ☐ **Supervisor** node + Feedback edge: Analyst proposes plan updates, Supervisor approves/rejects/critiques (still TBD — today the user is effectively the supervisor for Reviewer-proposed changes)
8. ☐ **Trigger / Push** channel (start with email or a single push surface) on Supervisor-approved updates only
9. **Validation gate:** *Did the last 5 pushes actually deserve to interrupt me?*

**Phase 3 — Layered roles & productize**
10. ☐ Challenger mode on Supervisor, triggered by your behavior (adding to an at-risk position)
11. ☐ Scheduled Coach (optional) — a weekly batch over Plan store + action history. May not be needed now that on-demand Reviewer exists; keep open until the daily flow tells us.
12. ☐ Multi-user, broker integrations, pricing, compliance

---

## ❓ Open Questions

1. ~~Reason status: binary / ternary / 0–100 score?~~ **Resolved: four-status enum** — `on_track / at_risk / off_track / ahead`. Asymmetric on purpose: `off_track` = adverse breach; `ahead` = symmetric upside that catches under-sized winners. Watcher and Reviewer both use these.
2. **One plan per ticker, or many** (long-term + tactical)?
3. Supervisor approves writes to the Plan store — but does it also auto-update reason `current_value`, or always ask the Analyst (and you) for sign-off?
4. **Info Storage retention:** rolling 30 days? Per-plan quota? Never delete (for replay)?
5. **Trigger/Push surface:** email, native push, a single "morning digest," or all three gated by severity?
6. First non-news signal beyond price: **SEC filings · insider flow · earnings transcripts?**
7. **Reviewer cost ceiling.** Each back-check is one strong-model call against a long context (history + financials + alerts + journal). Cheap for personal use; could matter at scale. Cache or rate-limit?

---

## 🎁 The Unfair Angle

Bloomberg, Seeking Alpha, and fintech apps **structurally cannot personalize to your portfolio + plans + behavior** — they serve millions. A small AI-native tool that knows *you*, watches on your behalf, and only interrupts when a reason actually moves is a moat they can't cross.

---

## ▶️ Next Session Decision Points

- [ ] Decide Supervisor's contract: what fields must a Plan Update carry for it to be approvable? (Reviewer already produces a clean `proposed_changes` schema — borrow from it.)
- [ ] Sketch Watcher + Reviewer unit economics — per-day cost on 10 holdings × 4 input domains, plus on-demand Reviewer runs
- [ ] Pick the first Trigger/Push surface (and the severity threshold that fires it)
- [ ] Resolve the remaining open questions above (Q1 done)
- [ ] Build Supervisor + Feedback loop to close the live loop properly (Watcher's status changes currently land directly without LLM-side approval)
