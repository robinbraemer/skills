---
name: query-local-discord-cache
description: Inspect, query, recover, or archive data from a local Discord desktop client on macOS. Use when locating Discord's Chromium cache, LevelDB, IndexedDB, or SQLite storage; finding locally cached messages; or optionally building a user-defined SQLite/FTS5 index for faster repeated searches.
---

# Query Local Discord Cache

Treat Discord's local profile as a collection of Chromium/Electron stores, not as one canonical message database. Discover what the installed client actually contains, inspect it read-only, and let the user's goal determine what—if anything—to archive.

## Find the profile

Look under `~/Library/Application Support/`. Stable commonly uses `discord`; PTB and Canary may use names such as `discordptb` and `discordcanary`. Discover matching directories instead of assuming the client or version.

Useful first passes include:

```sh
find "$HOME/Library/Application Support" -maxdepth 1 -type d -iname '*discord*' -print
du -sh "$HOME/Library/Application Support/discord"/* 2>/dev/null | sort -h
```

## Know the likely stores

- `Cache/Cache_Data`: Chromium HTTP-cache entries. Message-list responses may appear here with API URLs such as `/api/v*/channels/<channel-id>/messages`, response metadata, and gzip-compressed JSON bodies. Filenames are opaque and the cache format can vary by Chromium version.
- `Local Storage/leveldb`: persisted client state such as preferences, selected channels, drafts, search state, and account metadata. It can also contain authentication material. Do not assume it is the main message archive.
- `WebStorage`: origin and bucket storage, including IndexedDB, CacheStorage, and a SQLite `QuotaManager` that can help map numbered buckets to origins.
- `Session Storage`, `Service Worker`, and logs: useful for targeted investigations, but usually not durable chat history.
- SQLite-looking files such as `Cookies`, `DIPS`, notification caches, quota metadata, or updater databases: inspect their schemas before inferring relevance. A file named `.db` is not necessarily a message database.

Use `find`, `file`, `du`, `rg -a`, `strings`, and `sqlite3` to narrow the search. Prefer evidence from schemas, cache keys, endpoint URLs, and decoded samples over filenames alone.

## Inspect without disturbing Discord

Prefer read-only inspection. Discord may hold SQLite or LevelDB locks while running. For exploratory work, copy the complete store or directory to a temporary location and inspect the copy. Include SQLite sidecar files or the full LevelDB directory when present.

For a SQLite file where ignoring live WAL state is acceptable, an immutable connection can sometimes bypass application locks:

```sh
sqlite3 "file:/absolute/path/to/database?immutable=1" '.schema'
```

If consistency matters, explain the tradeoff: a live file copy can race with writes. Ask the user to quit Discord or use a consistent filesystem snapshot only when the requested accuracy requires it.

## Recover cached message responses

Search cache entries for Discord message endpoints, then identify the associated response body. Responses may be gzip-compressed JSON arrays or objects. Use a cache parser or a small task-specific decoder when needed; do not assume that printable strings are the complete body.

Keep the extraction proportional to the request. For example:

- Decode only one known channel when the user asks about that channel.
- Scan all matching message endpoints when building a broader archive.
- Deduplicate overlapping pagination responses by Discord message ID.
- Preserve source cache filenames or request URLs when provenance will matter.

Do not expose unrelated message content merely to prove that decoding worked.

## Consider SQLite and FTS only when useful

For a few lookups, querying decoded JSON directly may be enough. For repeated searches, pagination overlap, or a growing archive, suggest SQLite as an optimization—not a requirement.

Let the user choose the data model. Possible fields include message ID, timestamp, channel and guild IDs, author identifiers, content, attachments, embeds, raw JSON, and cache provenance. Retain only what serves the requested archive.

An ordinary table with a unique message ID supports deduplication. An optional FTS5 table can index whichever text fields the user wants, such as content and display names. External-content FTS with synchronization triggers is useful for a mutable archive; a one-time bulk archive may simply rebuild the index after import.

Avoid presenting a sample schema as canonical. Discord payloads evolve, and different investigations may care about reactions, threads, edits, embeds, attachments, or only plain text.

## Verify the result

Choose checks that match the task. Useful checks include:

- Every selected cache response decoded, or failures are listed explicitly.
- Message IDs are unique after deduplication.
- Counts and oldest/newest timestamps are plausible.
- Channel or guild scope matches the user's request.
- `PRAGMA integrity_check` succeeds for a created SQLite archive.
- Indexed row counts match source rows and at least one representative FTS query returns the expected record.

## Protect private data

Treat the entire Discord profile and any derived archive as sensitive.

- Never print token or cookie values.
- Avoid collecting unrelated private conversations.
- Keep local exports within the user's requested scope.
- Do not modify the live Discord profile.
- Remove temporary snapshots and decoders when they are no longer needed, unless the user asks to keep them.
