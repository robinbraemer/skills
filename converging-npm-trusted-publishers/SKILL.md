---
name: converging-npm-trusted-publishers
description: Use when configuring, reconciling, or resuming npm Trusted Publisher settings for an explicit package allowlist through a user's logged-in, visible Chrome profile.
---

# Converging npm Trusted Publishers

Converge only the allowlisted Trusted Publisher tuple. Treat every unexpected state as a stop, never as permission to repair or broaden scope.

## Hard boundaries

- **REQUIRED SUB-SKILL:** Use `browser` for browser-harness attachment guidance.
- Attach `browser-harness` to the user's real, visible Chrome profile. Never launch, copy, sync, inspect, export, or retain a profile.
- Never read, copy, log, or retain credentials, cookies, passwords, OTPs, passkeys, security-key/WebAuthn material, npm tokens, session/request identifiers, network payloads, or private endpoints. Never open network capture or developer tools around authentication.
- Never delete, revoke, replace, or edit an unexpected publisher. Never publish/stage/deprecate/delete packages, alter dist-tags or access policy, dispatch workflows, or touch unrelated settings.
- Do not use screenshots, JavaScript, `type_text`, `fill_input`, fixed coordinates, generic CDP, npm CLI trust commands, or direct HTTP.
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

1. Confirm browser-harness is already attached to the intended visible Chrome profile without printing the current tab, profile, or session details.
2. Resolve this skill directory and user-approved manifest/ledger paths. Keep the ledger local and out of version control.
3. Run the helper exactly through the restricted interface below. It opens one visible npm tab per package, globally inspects every package before the first write, skips exact matches, and processes absent publishers sequentially.
4. Warn the human before Save may trigger authentication. While the helper waits, do not issue browser input. The human alone completes or cancels authentication.
5. A nonzero exit is a safe stop. Report only package status and reason code. Fix no state automatically; resume by rerunning the same manifest and ledger after the human resolves the cause.
6. Claim success only on exit `0`, after the helper's final read-back sweep.

```bash
export SKILL_DIR MANIFEST LEDGER
browser-harness <<'PY'
import os
import runpy

module = runpy.run_path(os.path.join(os.environ["SKILL_DIR"], "scripts", "converge.py"))

def page_identity():
    info = page_info()
    return {"url": info["url"], "title": info["title"]}

api = {
    "new_tab": new_tab,
    "switch_tab": switch_tab,
    "wait_for_load": wait_for_load,
    "page_identity": page_identity,
    "accessibility_tree": lambda: cdp("Accessibility.getFullAXTree")["nodes"],
    "box_model": lambda node: cdp("DOM.getBoxModel", backendNodeId=node)["model"],
    "reload_page": lambda: cdp("Page.reload"),
    "click_at_xy": click_at_xy,
    "press_key": press_key,
}

raise SystemExit(module["run_browser_harness"](
    manifest_path=os.environ["MANIFEST"],
    ledger_path=os.environ["LEDGER"],
    api=api,
))
PY
```

`press_key(key, modifiers=0)` emits keyboard events. The helper sends one unmodified character at a time and requires a fresh accessibility prefix after every key. It never receives `type_text` or generic browser/session/network functions.

## Stop conditions

Stop the whole run on an unexpected existing publisher; package, origin, or URL mismatch; unsupported action; missing/ambiguous/disabled control; UI drift; staged-form mismatch; failed, canceled, timed-out, or ambiguous authentication; missing success acknowledgement; partial save; or read-back mismatch. Do not retry, delete, overwrite, continue to another package, or treat a banner alone as proof.

On resume, the manifest digest must match. Every package is reopened and read from npm; ledger state is never trusted as npm state.

## Quick reference

| Result | Meaning |
|---|---|
| `0` | Every package matched in the final sweep. |
| `2` | Invalid manifest or ledger; no browser write. |
| `3` | Preflight refused; no browser write. |
| `4` | Resumable stop after staging/save began. |
| `5` | Restricted harness/API failure; fail closed. |

The ledger contains only manifest package names, a manifest digest, fixed statuses, and fixed reason codes—never URLs, observed publisher values, page content, screenshots, timestamps, account/auth details, or browser/session/network material.

## Common mistakes

| Rationalization | Required response |
|---|---|
| “The release is urgent; replace the mismatch.” | Stop. Existing publishers are never changed or deleted. |
| “Authentication failed only here; continue the next package.” | Stop the whole run and preserve resumable status. |
| “The success banner is enough.” | Require reload, exact read-back, and final sweep. |
| “The redesign is cosmetic; use old coordinates or DOM.” | Stop on semantic UI drift. |
| “Keep screenshots or URLs as evidence.” | Keep only the redacted ledger contract. |

## Red flags

Any proposal to bypass the helper, broaden the allowlist, handle human authentication, inspect browser/session/network data, continue after a stop, or repair an unexpected publisher means: **stop without another browser action**.
