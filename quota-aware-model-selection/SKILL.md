---
name: "quota-aware-model-selection"
description: "Use when an agent, router, dispatcher, or automation is choosing between models, providers, accounts, quota pools, or backends using quota data such as quota-axi output."
version: 1
created: "2026-07-09"
updated: "2026-07-09"
---
## When to Use
Use when deciding where an AI task should run and quota, provider limits, model quality, account pools, or reset windows may affect the choice. This is especially suitable for agents or routers consuming [`quota-axi`](https://github.com/kunchenguid/quota-axi) CLI output. Applies to automatic routers and human-facing recommendations. Do not use for merely reporting quota without making or explaining a selection.

## Procedure
1. Treat model/task fit as the primary factor: choose a model capable of the work before optimizing quota. Quota is one input, not the whole decision.
2. When changing provider/model would affect quality, latency, cost, safety, permissions, or user expectations, explain the tradeoff or ask the user/captain unless a standing policy already authorizes it.
3. When choosing between equivalent accounts for the same provider/model/capability, automation may decide without asking. Optimize for effective usable quota over time, not just the lowest percentage.
4. For equivalent Codex accounts, automatic selection is appropriate. Prefer the account whose remaining quota is most urgent to spend before reset: high remaining quota with a near reset can outrank lower remaining quota with a later reset.
5. If reset urgency is similar, then drain the account with the least weekly quota remaining, as long as it still has enough headroom for the task; fall back to fuller accounts when the lower one is exhausted, stale, rate-limited, unavailable, or lacks required capability.
6. Use reported reset times, freshness, provider status, and per-window limits from quota tools such as [`quota-axi`](https://github.com/kunchenguid/quota-axi). Prefer live/fresh quota data; be conservative with stale or unknown data.
7. Do not assume a provider's weekly reset starts after first use unless that exact provider/window is confirmed by current docs or observed quota data.
8. Do not silently spend a “primer hit” on a 100% full account just to start a reset timer. Only do this if an explicit policy enables it for a confirmed first-use rolling window; otherwise surface the option to the user/captain.
9. When user/captain makes a selection, follow it even if the quota heuristic would pick differently, unless it is impossible or unsafe.

## Example Scenarios
- Same model, Account A has 40% weekly left and resets in 3 days; Account B has 99% weekly left and resets in 1 day. Use Account B: its quota is about to reset, so spend the high remaining quota before it is wasted.
- Same model, Account A has 40% weekly left and resets in 3 days; Account B has 100% weekly left and no reset clock yet. Use Account A, unless an explicit policy permits a tiny primer hit on B for a confirmed first-use rolling window.
- Same model, Account A has 5% weekly left and resets in 5 hours; Account B has 80% weekly left and resets in 6 days. Use Account A for small tasks if it has enough headroom; otherwise switch to B.
- Same model, Account A has 5% weekly left and resets soon, but the task likely needs 20%. Do not start on A and fail mid-task; use an account with enough headroom.
- Same account has plenty of weekly quota but its 5-hour/session window is nearly exhausted. Route by the tighter active window, not just the weekly window.
- Same model, quota data is stale or unknown. Do not make aggressive routing or primer decisions; refresh quota or choose the conservative default.
- Same model, Account A has 2% left and resets in 30 minutes. Use it only for tiny tasks that fit safely; otherwise avoid interruption risk.
- Multiple agents are dispatching in parallel. Avoid stampeding the same "best" account; use coordination/locking or re-check quota after assigning work.
- Different models, stronger model has low quota and weaker model has plenty. Do not auto-downgrade if task quality would suffer; ask or follow standing policy.

## Pitfalls
- Do not let quota optimization downgrade the model below what the task needs.
- Do not compare percentages across different providers as if 10% of one provider equals 10% of another.
- Do not mutate provider state, launch provider CLIs that spend quota, import cookies, or proxy requests just to inspect quota.
- Do not make primer-hit behavior the default; it is intentional quota spend.
- Do not ask the user for routine equivalent-account choices when policy and data make the answer clear.
- Do not ignore smaller active windows, such as 5-hour/session/model-specific limits, when weekly quota looks healthy.
- Do not route parallel work from stale shared quota snapshots without coordination.

## Verification
1. Selection rationale names the decisive factors: model fit first, quota/account choice second.
2. Equivalent-account routing, especially Codex-to-Codex, can be automatic and chooses the account with the most urgent useful quota to spend: near-reset high remaining quota first, otherwise lowest remaining quota with enough headroom.
3. Any cross-model/provider change is either covered by standing policy or surfaced to the user/captain.
4. No unapproved primer hit or provider-state mutation occurred.
5. The selected account has enough headroom in every relevant active window for the expected task size.
6. Parallel dispatchers either coordinated account assignment or refreshed quota before routing.