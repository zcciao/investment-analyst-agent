# Glossary

The vocabulary used across the app, the code, and the data files. Plain English first; the technical "previous" term is noted in italics so finance- or codebase-fluent readers can map between the two.

This doc is the source of truth — the in-app Glossary screen mirrors it.

---

## The big idea

This isn't a stock picker. It's a **plan tracker**: you write down *why* you own a position, the app watches the world, and it only interrupts you when something would actually change your mind.

---

## Core concepts

### Plan
Your structured reason for holding a position. *(Some apps call this a thesis.)*

A plan has:
- **One-liner** — the bull case in under 15 words
- **Direction** — `long` or `short`
- **Confidence** — your belief on a 1–10 scale
- **Time horizon** — free-form, e.g. `12–18 months`
- **Entry price + date** — drives P&L and the back-check window
- **Reasons** — the load-bearing claims (see below)
- **Upcoming events** — known future moves (earnings, launches)
- **Exit triggers** — when to bail out (see below)
- **Journal** — running log of revisions and observations

### Reason
A load-bearing claim that *has to be true* for the plan to work. *(Some apps call this a pillar.)*

Each reason has:
- **Name** — short label, e.g. *"Cloud growth"*
- **Current value** — latest factual reading, e.g. *"Cloud growth 31% YoY (Q1 2026 release)"*
- **Walk-away signal** — the measurable condition that would invalidate this reason. *(Previously: `threshold_break`.)* Examples: *"<25% growth for 2 quarters"*, *"P/E > 40x sustained"*. Concrete and falsifiable.
- **Data sources** — what to watch for changes. *(Previously: `watch_signals`.)* Examples: 10-Qs, earnings calls, industry indices.
- **Status** — one of four states (see below)

### Confidence
Your belief in the plan, **1–10**. *(Previously: `conviction`.)* Higher = stronger. Used to weight portfolio-level numbers and to flag when you're adding to a low-confidence position.

### Upcoming event
A known future event that could move the plan — earnings calls, product launches, regulatory decisions. *(Previously: `catalyst`.)* They show up on Briefing as a heads-up before they happen.

### Exit triggers
When to bail out. *(Previously: `exit_plan`.)* Three fields:
- **Take profit** — target price or condition for realizing gains
- **Plan break** — the condition that signals the plan is invalidated. Logic-tied stop, not price-tied. *(Previously: `thesis_break`.)*
- **Time stop** — deadline at which to re-evaluate if the plan hasn't played out

---

## Reason status

Each reason is in one of four states. The watcher updates them; you read them.

| Status | Meaning | Colour | Previously |
|---|---|---|---|
| **`on_track`** | The reason is holding clear of its walk-away signal. Resting state. | emerald | `intact` |
| **`at_risk`** | Slipping toward the walk-away signal but not yet crossed. One more weak data point could break it. | amber | `wobbling` |
| **`off_track`** | Walk-away signal triggered *in the wrong direction*. The "why" no longer holds. Revise or exit. | rose | `broken` |
| **`ahead`** | Metric is **materially better** than expected — well clear of the failure condition, with material improvement vs. the recorded value. Consider raising the walk-away signal or your confidence. | teal | `strengthening` |

**Asymmetric for a reason.** `off_track` covers only adverse breaches. Numbers exceeding the bullish case are *never* `off_track` — they're `ahead` or remain `on_track`. This catches under-sized winners that would otherwise stay invisible.

---

## Portfolio health

The **Portfolio Health %** in the Briefing is the share of all your reasons that are `on_track` or `ahead`. 100% = nothing off-track. The ring turns amber below 80%, red below 50%.

---

## The agents

Four agents share the workload. Each one has a clock, a role, and a cost.

### 🔭 Watcher
**Clock:** continuous (cron, plus on-demand from the UI).
**Role:** forward-looking. Scans markets, news, social, and fundamentals against each reason's data sources. Emits one of the four statuses per reason. Pre-filters before invoking the LLM to keep costs near zero.
**Cost:** cheap, small model.

### 🔬 Analyst
**Clock:** on-demand. Triggered by the user in chat (the **Ask** tab).
**Role:** conversational research. Pulls live data, explains plan state, flags risk, and **explicitly prompts revision** when a reason goes `off_track` or `ahead`. You inform; the analyst informs.
**Cost:** strong model, rare.

### 🩺 Reviewer
**Clock:** on-demand. Triggered from a plan's detail page via **🔍 Review this plan**. *(Previously: Validator; back-check.)*
**Role:** retrospective grading. Pulls price history + fundamentals over the holding window, grades each reason with a verdict (see below), and proposes structured updates the user can apply or discard. Writes nothing directly — proposes only.
**Cost:** strong model, rare.

### 🛡️ Supervisor
**Clock:** per agent output.
**Role:** quality gate. Before any change touches the plan store, the supervisor checks: is the evidence cited? Is the reasoning sound? Is the change warranted? **Approve** → committed. **Reject** → analyst tries again with feedback. *(Today the user is effectively the supervisor for Reviewer-proposed changes; the agent-side supervisor is on the roadmap.)*
**Cost:** strong model, scoped.

---

## Reviewer verdicts

When you back-check a plan, each reason is graded:

| Verdict | Meaning | Previously |
|---|---|---|
| **`held_up`** | Walk-away signal has not been triggered AND the underlying claim is still supported. | `held` |
| **`failed`** | The walk-away signal was triggered at some point. | `missed` |
| **`set_too_low`** | The signal never came close to triggering, even as the metric weakened — it's not actually load-bearing. Widen or sharpen it. | `too_loose` |
| **`set_too_high`** | The signal triggered on normal noise rather than plan-breaking moves. Loosen it. | `too_tight` |
| **`not_enough_data`** | Not enough time/data has passed to judge. | `untested` |

The reviewer also produces:
- **Narrative** (3–5 paragraphs)
- **Performance summary** — return, duration, max drawdown, max runup
- **Lessons** — short actionable bullets
- **Proposed changes** — structured edits (new walk-away signals, new confidence, etc.) you can apply with one tap
- **Recommendation** — `hold / exit / add / watch / revise`

---

## Data model (for code readers)

The Pydantic models live in `agent/plan_store.py`:

```python
ReasonStatus = Literal["on_track", "at_risk", "off_track", "ahead"]

class Reason:
    name: str
    current_value: str
    walk_away_signal: str
    data_sources: list[str]
    status: ReasonStatus = "on_track"

class Event:
    name: str
    expected_date: str | None

class ExitTriggers:
    take_profit: str | None
    plan_break: str | None
    time_stop: str | None

class Plan:
    ticker: str
    one_liner: str
    direction: Literal["long", "short"]
    confidence: int  # 1–10
    time_horizon: str
    entry_price: float | None
    entry_date: str | None
    reasons: list[Reason]
    events: list[Event]
    exit_triggers: ExitTriggers
    source_material: str | None
    journal: list[str]
    last_updated: str
```

JSON keys match the model fields. Alerts use `reason_name` (not `pillar_name`).

---

## Old → new term map (one-table cheat sheet)

| Old | New | Where it appears |
|---|---|---|
| thesis | **plan** | object name, route prefix, UI tab |
| pillar | **reason** | field, UI |
| catalyst | **event** | field, UI |
| conviction | **confidence** | field, UI |
| threshold_break | **walk_away_signal** | JSON field |
| watch_signals | **data_sources** | JSON field |
| exit_plan | **exit_triggers** | JSON field |
| thesis_break | **plan_break** | nested field in exit_triggers |
| intact | **on_track** | status enum value |
| wobbling | **at_risk** | status enum value |
| broken | **off_track** | status enum value |
| strengthening | **ahead** | status enum value |
| held | **held_up** | reviewer verdict |
| missed | **failed** | reviewer verdict |
| too_loose | **set_too_low** | reviewer verdict |
| too_tight | **set_too_high** | reviewer verdict |
| untested | **not_enough_data** | reviewer verdict |
| Validator | **Reviewer** | agent name |
| validate / back-check | **review** | endpoint, button |

If you're reading old commits, journal entries, or any agent output from before the rename, that's the mapping.
