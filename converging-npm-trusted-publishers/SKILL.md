---
name: converging-npm-trusted-publishers
description: Use when configuring, reconciling, or resuming npm Trusted Publisher settings for an explicit package allowlist through a user's logged-in, visible Chrome profile.
---

# Converging npm Trusted Publishers

Converge only the allowlisted Trusted Publisher tuple. Treat every unexpected state as a stop, never as permission to repair or broaden scope.

## Hard boundaries

- **REQUIRED SUB-SKILL:** Use `browser-with-axi` for the pinned chrome-devtools-axi invocation rule. Only the locally vendored, reviewed release (`bun "$HOME/.codex/tools/chrome-devtools-axi-0.1.29/dist/bin/chrome-devtools-axi.js"`) may drive the browser. Never `npx`, unversioned `bunx`, `@latest`, or AXI's `update` command.
- Attach the AXI bridge to the user's real, visible Chrome (`CHROME_DEVTOOLS_AXI_AUTO_CONNECT=1` against the running profile). Never launch, copy, sync, inspect, export, or retain a profile, and never create an isolated profile for this task.
- Never read, copy, log, or retain credentials, cookies, passwords, OTPs, passkeys, security-key/WebAuthn material, npm tokens, session/request identifiers, network payloads, or private endpoints. Never open network capture or developer tools around authentication.
- Never delete, revoke, replace, or edit an unexpected publisher. Never publish/stage/deprecate/delete packages, alter dist-tags or access policy, dispatch workflows, or touch unrelated settings.
- The helper enforces an AXI command allowlist: `newpage`, `selectpage`, `pages`, `snapshot`, `click @<uid>`, `press <key>`, and `open` of the canonical package URL (the reload equivalent). Everything else is refused before any subprocess is spawned — no `eval`/`run` (JavaScript), no `fill`/`fillform`/`type`, no `screenshot`, no `console`/`network`/`network-get`, no `heap`/`lighthouse`/`perf-*`, no `update`. Text entry uses per-character `press` with a fresh accessibility read-back after every key; `fill` is rejected even for the non-credential owner/repository/workflow fields so that no code path capable of bulk-writing an input exists at all.
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

## Workflow

1. Confirm the pinned AXI bridge is already attached to the intended visible Chrome profile without printing the current tab, profile, or session details. If the vendored install is missing, stop and follow `browser-with-axi` — do not substitute another driver.
2. Resolve this skill directory and user-approved manifest/ledger paths. Keep the ledger local and out of version control.
3. Run the helper. It opens one visible npm tab per package, globally inspects every package before the first write, skips exact matches, and processes absent publishers sequentially.
4. Warn the human before Save may trigger authentication. While the helper waits, do not issue browser input. The human alone completes or cancels authentication.
5. A nonzero exit is a safe stop. Report only package status and reason code. Fix no state automatically; resume by rerunning the same manifest and ledger after the human resolves the cause.
6. Claim success only on exit `0`, after the helper's final read-back sweep.

```bash
python3 "$SKILL_DIR/scripts/converge.py" --manifest "$MANIFEST" --ledger "$LEDGER"
```

The helper resolves the pinned CLI at `${CODEX_HOME:-$HOME/.codex}/tools/chrome-devtools-axi-0.1.29/dist/bin/chrome-devtools-axi.js` and runs it through `bun`; `--axi-script` and `--bun` exist only to point at the same reviewed, pinned artifacts in a nonstandard location, never at a newer version. Requires Python 3 and exits `5` before any browser action when the pinned CLI is absent.

## Stop conditions

Stop the whole run on an unexpected existing publisher; package, origin, or URL mismatch; unsupported action; missing/ambiguous/disabled control; UI drift (including an unparseable or truncated accessibility snapshot, or a ref that stays stale after one re-snapshot); staged-form mismatch; failed, canceled, timed-out, or ambiguous authentication; missing success acknowledgement; partial save; or read-back mismatch. Do not retry, delete, overwrite, continue to another package, or treat a banner alone as proof.

On resume, the manifest digest must match. Every package is reopened and read from npm; ledger state is never trusted as npm state.

## Quick reference

| Result | Meaning |
|---|---|
| `0` | Every package matched in the final sweep. |
| `2` | Invalid manifest or ledger; no browser write. |
| `3` | Preflight refused; no browser write. |
| `4` | Resumable stop after staging/save began. |
| `5` | Pinned CLI missing or restricted transport failure; fail closed. |

The ledger contains only manifest package names, a manifest digest, fixed statuses, and fixed reason codes—never URLs, observed publisher values, page content, screenshots, timestamps, account/auth details, or browser/session/network material.

## Common mistakes

| Rationalization | Required response |
|---|---|
| “The release is urgent; replace the mismatch.” | Stop. Existing publishers are never changed or deleted. |
| “Authentication failed only here; continue the next package.” | Stop the whole run and preserve resumable status. |
| “The success banner is enough.” | Require reload, exact read-back, and final sweep. |
| “The redesign is cosmetic; use old coordinates or DOM.” | Stop on semantic UI drift. |
| “`fill` is faster for these harmless text fields.” | No. Per-key `press` with read-back is the only text path; `fill` stays outside the allowlist. |
| “The ref is stale again; keep re-snapshotting until it works.” | One retry only, then stop. Repeated churn is UI drift. |
| “Keep screenshots or URLs as evidence.” | Keep only the redacted ledger contract. |

## Red flags

Any proposal to bypass the helper, invoke AXI through `npx`/`bunx`/`update`, broaden the command allowlist, handle human authentication, inspect browser/session/network data, run JavaScript, continue after a stop, or repair an unexpected publisher means: **stop without another browser action**.
