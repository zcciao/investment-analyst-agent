# Personal Investment AI — Strategic One-Pager

**Stage:** Brainstorm review · **Mode:** Build for self first → productize if it works

---

## 🎯 The Reframe

> **A research analyst that knows your portfolio, your theses, and your past mistakes — and tells you only what would change your mind.**

Not another information aggregator. Not a robo-advisor. A **thesis tracker with a watcher and a devil's advocate.** The product never makes decisions for you — it makes you a sharper decider.

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
  - Status: `intact` / `wobbling` / `broken`
- **Catalysts** = known future events (earnings, launches)
- **Exit plan** = take-profit · thesis-break · time-stop
- **Source material** = the article/video that seeded the thesis
- **Journal + revisions** = living document

**Killer UX:** paste a YouTube post → AI extracts draft pillars → you accept/edit. No YAML by hand.

---

## 🤖 The Agent Architecture

Four agents earn their keep because they have **different clocks, different evidence sources, or different inputs**. Everything else is a *role*, not an agent.

| Agent | Clock | Role | Cost |
|---|---|---|---|
| 🔭 **Watcher** | Continuous (cron) | Scan watch_signals; raise candidates only | Cheap, small model |
| 🔬 **Analyst** | On-demand | Deep dive when triggered. Internal roles: Fundamentals / Sentiment / News / Technical | Strong model, rare |
| ⚔️ **Challenger** | Big actions + behavior triggers | Adversarial bear case with its own evidence | Top-tier model, rare |
| 🪞 **Coach** | Weekly | Reviews **you** — exit patterns, ignored alerts, calibration | Strong model, scheduled |

**No "Decider" agent.** You make every trade. Agents inform, never act.

**The defensible move:** Challenger triggered by *your* behavior, not the market's.
> *"You're adding to GOOG. 2 pillars have weakened this month. Want the bear case before you add?"*

---

## 📚 vs. TradingAgents (the OSS benchmark)

| | TradingAgents | Your product |
|---|---|---|
| State | Stateless | Stateful (theses + behavior) |
| Output | BUY/SELL/HOLD | Perspective + alerts |
| Trigger | Pull (you invoke) | Push (it watches) |
| Personalization | None | Core moat |
| Best for | Research / backtesting | Living portfolio |

**Borrow:** bull/bear debate, role decomposition, LangGraph orchestration
**Don't copy:** stateless runs, decision-maker framing
**Combo strategy:** use TradingAgents as on-demand deep-analysis engine; wrap in your personalization layer.

> *TradingAgents is a Bloomberg Terminal. You're building a personal CFO.*

---

## ⚠️ Landmines (decide early)

1. **Hallucinated numbers = real money lost.** Cite every figure; refuse to answer if uncertain.
2. **Cost runaway.** Watcher must be near-free. Pre-filter before any LLM call.
3. **Latency theater.** Most queries <5s. Save multi-agent debates for triggered moments.
4. **Council that never decides.** Each agent commits with a confidence score.
5. **Privacy.** Portfolio + thesis = sensitive. Plan for it before productizing.

---

## 🪜 Build Sequence

**Phase 1 — Self-only (4–8 weeks)**
1. Thesis object + paste-to-pillars import flow
2. Watcher MVP on 5–10 holdings, daily run
3. Conversational interface ("what's up with GOOG?")
4. **Validation gate:** *Am I opening it weekly without forcing myself?*

**Phase 2 — Add depth (after Phase 1 sticks)**
5. Analyst with role decomposition
6. Challenger with behavioral triggers
7. Coach with weekly digest

**Phase 3 — Productize (only if Phase 1+2 stick)**
8. Multi-user, broker integrations, pricing, compliance

---

## ❓ Open Questions

1. Pillar status: **binary / ternary / 0–100 score?**
2. **One thesis per ticker, or many** (long-term + tactical)?
3. AI auto-updates `current_value`, or always asks for sign-off?
4. First non-news signal beyond price: **SEC filings · insider flow · earnings transcripts?**
5. Productize as **power-user tool** or **simpler retail product?**

---

## 🎁 The Unfair Angle

Bloomberg, Seeking Alpha, and fintech apps **structurally cannot personalize to your portfolio + thesis + behavior** — they serve millions. A small AI-native tool that knows *you* is a moat they can't cross.

---

## ▶️ Next Session Decision Points

- [ ] Test the thesis schema by filling in a real GOOG thesis (using the 美投 post as source)
- [ ] Sketch Watcher unit economics — what's the per-day cost on 10 holdings?
- [ ] Resolve the 5 open questions above
- [ ] Pick: do we sketch the **import flow** or the **Watcher** as the first build target?
