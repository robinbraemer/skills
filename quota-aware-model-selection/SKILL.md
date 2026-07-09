---
name: "quota-aware-model-selection"
description: "Use when an agent, router, dispatcher, or automation is choosing between models, providers, accounts, quota pools, or backends using quota data such as quota-axi output."
version: 1
created: "2026-07-09"
updated: "2026-07-09"
---
## When to Use
Use when deciding where an AI task should run and quota, provider limits, model quality, account pools, or reset windows may affect the choice. This is especially suitable for agents or routers consuming `quota-axi` CLI output. Applies to automatic routers and human-facing recommendations. Do not use for merely reporting quota without making or explaining a selection.

## Procedure
1. Treat model/task fit as the primary factor: choose a model capable of the work before optimizing quota. Quota is one input, not the whole decision.
2. When changing provider/model would affect quality, latency, cost, safety, permissions, or user expectations, explain the tradeoff or ask the user/captain unless a standing policy already authorizes it.
3. When choosing between equivalent accounts for the same provider/model/capability, automation may decide without asking. Prefer the account with the least weekly quota remaining, as long as it still has enough headroom for the task.
4. For equivalent Codex accounts, automatic selection is appropriate: use the Codex account with the least weekly quota remaining; fall back to fuller accounts when the lower one is exhausted, stale, rate-limited, unavailable, or lacks required capability.
5. Use reported reset times, freshness, provider status, and per-window limits from quota tools such as `quota-axi`. Prefer live/fresh quota data; be conservative with stale or unknown data.
6. Do not assume a provider's weekly reset starts after first use unless that exact provider/window is confirmed by current docs or observed quota data.
7. Do not silently spend a “primer hit” on a 100% full account just to start a reset timer. Only do this if an explicit policy enables it for a confirmed first-use rolling window; otherwise surface the option to the user/captain.
8. When user/captain makes a selection, follow it even if the quota heuristic would pick differently, unless it is impossible or unsafe.

## Pitfalls
- Do not let quota optimization downgrade the model below what the task needs.
- Do not compare percentages across different providers as if 10% of one provider equals 10% of another.
- Do not mutate provider state, launch provider CLIs that spend quota, import cookies, or proxy requests just to inspect quota.
- Do not make primer-hit behavior the default; it is intentional quota spend.
- Do not ask the user for routine equivalent-account choices when policy and data make the answer clear.

## Verification
1. Selection rationale names the decisive factors: model fit first, quota/account choice second.
2. Equivalent-account routing, especially Codex-to-Codex, can be automatic and chooses the lowest remaining weekly quota with enough headroom.
3. Any cross-model/provider change is either covered by standing policy or surfaced to the user/captain.
4. No unapproved primer hit or provider-state mutation occurred.