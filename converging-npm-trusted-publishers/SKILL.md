---
name: converging-npm-trusted-publishers
description: Use when configuring, reconciling, resuming, or migrating npm Trusted Publisher settings for an explicit package allowlist through a user's logged-in, visible Chrome profile.
---

# Converging npm Trusted Publishers

Converge only the allowlisted Trusted Publisher tuple. Treat every unexpected state as a stop, never as permission to repair or broaden scope.

## Hard boundaries

- **REQUIRED SUB-SKILL:** Use `browser-with-axi` for the pinned chrome-devtools-axi invocation rule. Only the locally vendored, reviewed release (`bun "$HOME/.codex/tools/chrome-devtools-axi-0.1.29/dist/bin/chrome-devtools-axi.js"`) may drive the browser. Never `npx`, unversioned `bunx`, `@latest`, or AXI's `update` command.
- Attach the AXI bridge to the user's real, visible Chrome (`CHROME_DEVTOOLS_AXI_AUTO_CONNECT=1` against the running profile). Never launch, copy, sync, inspect, export, or retain a profile, and never create an isolated profile for this task.
- Never read, copy, log, or retain credentials, cookies, passwords, OTPs, passkeys, security-key/WebAuthn material, npm tokens, session/request identifiers, network payloads, or private endpoints. Never open network capture or developer tools around authentication.
- **Never delete or revoke a publisher.** The page's Delete control is verified present but never clicked, under any circumstances. Never edit a publisher whose tuple matches neither the target nor the manifest's explicitly approved `previous_publisher`; an approved previous tuple may only be rewritten to the target through the page's own in-place Edit form. Never publish/stage/deprecate/delete packages, alter dist-tags or access policy, dispatch workflows, or touch unrelated settings.
- The helper enforces an AXI command allowlist: `newpage`, `selectpage`, `pages`, `snapshot`, `click @<uid>`, `press <key>`, and `open` of the canonical package URL (the reload equivalent). Everything else is refused before any subprocess is spawned — no `eval`/`run` (JavaScript), no `fill`/`fillform`/`type`, no `screenshot`, no `console`/`network`/`network-get`, no `heap`/`lighthouse`/`perf-*`, no `update`. Text entry uses per-character `press` with a fresh accessibility read-back after every key; clearing a prefilled field during an authorized migration additionally allows exactly the named keys `End` and `Backspace`, each verified by an exact value read-back. `fill` is rejected even for the non-credential owner/repository/workflow fields so that no code path capable of bulk-writing an input exists at all.
- Elements are targeted only by accessible role and name from a fresh `snapshot --full`; generation-tagged refs (`@g<N>:<id>`) are passed back exactly as printed and never fabricated. A `STALE_REF` answer gets exactly one fresh re-snapshot retry, then the run fails closed. Fixed coordinates, DOM selectors, npm CLI trust commands, and direct HTTP are never used.
- Only the human may act on WebAuthn, passkey, security-key, OTP, password, consent, or account-selection UI.

## Manifest

Require a user-approved JSON file. One tuple applies to every package:

```json
{
  "schema_version": 1,
  "packages": ["@example/widgets"],
  "publisher": {
    "owner": "example-org",
    "repository": "widgets",
    "workflow": "release.yml",
    "environment": null,
    "allowed_actions": ["npm publish"]
  }
}
```

Allowed actions are exactly `npm publish` and `npm stage publish`. The workflow is a filename, not a path. Do not normalize case or infer omitted values.

`schema_version: 2` additionally permits an optional `previous_publisher` object (`owner`, `repository`, `workflow`, `environment`; `allowed_actions` may be omitted or must equal the target's). It is the one explicitly user-approved tuple the helper may migrate away from, and it must differ from the target. Without `previous_publisher`, v2 behaves exactly like v1.

Per-package semantics after the global inspect:

| Current npm state | Action |
|---|---|
| Absent | Create the target tuple (as v1). |
| Exactly the target | Skip; no writes. |
| Exactly `previous_publisher` | Rewrite in place to the target via the page's Edit form. |
| Anything else | Hard stop (`unexpected-publisher`); never edit, never delete. |

The existing-connection summary never displays an environment, so only environment-less previous tuples can ever match; a `previous_publisher` with a non-null environment therefore always stops.

## Workflow

1. Confirm the pinned AXI bridge is already attached to the intended visible Chrome profile without printing the current tab, profile, or session details. If the vendored install is missing, stop and follow `browser-with-axi` — do not substitute another driver.
2. Resolve this skill directory and user-approved manifest/ledger paths. Keep the ledger local and out of version control.
3. Run the helper. It opens one visible npm tab per package (closing its own tabs again when the run ends — never the user's), waits briefly for each page to finish rendering, globally inspects every package before the first write, skips exact matches, and processes creations and approved migrations sequentially.
4. Warn the human before Save may trigger authentication — for both new connections and migration edits. While the helper waits, do not issue browser input. The human alone completes or cancels authentication.
5. A nonzero exit is a safe stop. Report only package status and reason code. Fix no state automatically; resume by rerunning the same manifest and ledger after the human resolves the cause.
6. Claim success only on exit `0`, after the helper's final read-back sweep.

```bash
python3 "$SKILL_DIR/scripts/converge.py" --manifest "$MANIFEST" --ledger "$LEDGER"
```

The helper resolves the pinned CLI at `${CODEX_HOME:-$HOME/.codex}/tools/chrome-devtools-axi-0.1.29/dist/bin/chrome-devtools-axi.js` and runs it through `bun`; `--axi-script` and `--bun` exist only to point at the same reviewed, pinned artifacts in a nonstandard location, never at a newer version. Requires Python 3 and exits `5` before any browser action when the pinned CLI is absent.

Optional flags: `--stop-before-save` stages and verifies the first pending package, then exits `4` before Save so the human can be briefed before any authentication; resume by rerunning without the flag. `--debug-classify` prints redaction-safe classifier diagnostics (fixed booleans and the observed publisher tuple, which is public) when a package classifies unexpectedly.

**Bridge recovery.** Chrome's gated remote debugging (Chrome 144+, `chrome://inspect/#remote-debugging`) can silently stop accepting bridge connections — typically after Chrome restarts or the approval lapses — and every AXI command then fails with `BRIDGE_NOT_READY` ("attached CDP target appears to have gone away") even though the port is listening. This is a safe stop, not drift. Recovery is human-mediated: run `chrome-devtools-axi stop` for the session, have the user open `chrome://inspect/#remote-debugging`, re-enable/approve remote debugging, and **keep that tab open for the duration of the run** — gated debugging accepts new connections only while the page is open, so closing it wedges the next attach even though the port keeps listening. Then rerun. Never work around it by launching a separate profile or an unpinned driver.

**One attach, no reconnect polling.** In gated mode every connection attempt can queue a user-facing access-request prompt in Chrome. Retry loops that probe the debugging endpoint or cycle `stop`/reattach flood the user with popups and can block their WebAuthn prompts. Attach once, keep the single bridge for the whole run, and wait for the human to say the browser is ready instead of polling the endpoint.

**Human authentication needs the real Chrome frontmost.** npm gates the access-settings pages behind a session 2FA step-up, and macOS attaches the Touch ID sheet only to the focused real Chrome window — a click relayed while another app (terminal, embedded browser pane) is frontmost leaves the request pending with no visible prompt. Have the user bring Chrome itself to the front before any step-up or per-save security-key confirmation.

## Migration safety

A migration package is marked `migrating` in the ledger before the Edit form is opened and keeps that status until the post-save read-back verifies the target. The Edit form's prefilled values must exactly equal the approved previous tuple before any keystroke; the single mutating step is the form's Save. If a resumed run finds a `migrating` package whose publisher is now **gone**, it stops the whole run with `migration-interrupted` and never re-creates automatically — the human must verify npm state first (finishing manually and rerunning, or, after verification, starting a fresh ledger to re-authorize create-from-absent). A `migrating` package whose previous tuple survived intact is re-migrated; one already showing the target is verified and cleared.

## Stop conditions

Stop the whole run on a publisher matching neither the target nor the approved previous tuple; package, origin, or URL mismatch; unsupported action; missing/ambiguous/disabled control; UI drift (including an unparseable or truncated accessibility snapshot, a ref that stays stale after one re-snapshot, or an Edit form that does not show the approved previous tuple); staged-form mismatch; a resumed migration whose publisher is gone; failed, canceled, timed-out, or ambiguous authentication; missing success acknowledgement; partial save; or read-back mismatch. Do not retry, delete, overwrite, continue to another package, or treat a banner alone as proof.

On resume, the manifest digest must match. Every package is reopened and read from npm; ledger state is never trusted as npm state.

## Quick reference

| Result | Meaning |
|---|---|
| `0` | Every package matched in the final sweep. |
| `2` | Invalid manifest or ledger; no browser write. |
| `3` | Preflight refused (including an interrupted migration found on resume); no browser write. |
| `4` | Resumable stop after staging/save began. |
| `5` | Pinned CLI missing or restricted transport failure; fail closed. |

The ledger contains only manifest package names, a manifest digest, fixed statuses, and fixed reason codes—never URLs, observed publisher values, page content, screenshots, timestamps, account/auth details, or browser/session/network material.

## Common mistakes

| Rationalization | Required response |
|---|---|
| “The release is urgent; replace the mismatch.” | Stop. Only the explicitly approved previous tuple may be migrated, and only via in-place Edit. |
| “Delete + re-create is equivalent to editing.” | No. The Delete control is never clicked; editing keeps the connection existing at every instant. |
| “The publisher vanished mid-migration; just create the target.” | Stop (`migration-interrupted`). The human verifies npm state before anything is created. |
| “Authentication failed only here; continue the next package.” | Stop the whole run and preserve resumable status. |
| “The success banner is enough.” | Require reload, exact read-back, and final sweep. |
| “The redesign is cosmetic; use old coordinates or DOM.” | Stop on semantic UI drift. |
| “`fill` is faster for these harmless text fields.” | No. Per-key `press` with read-back is the only text path; `fill` stays outside the allowlist. |
| “The ref is stale again; keep re-snapshotting until it works.” | One retry only, then stop. Repeated churn is UI drift. |
| “Keep screenshots or URLs as evidence.” | Keep only the redacted ledger contract. |

## Red flags

Any proposal to bypass the helper, invoke AXI through `npx`/`bunx`/`update`, broaden the command allowlist, click the Delete control, migrate a tuple the manifest did not explicitly approve as `previous_publisher`, handle human authentication, inspect browser/session/network data, run JavaScript, continue after a stop, or repair an unexpected publisher means: **stop without another browser action**.
