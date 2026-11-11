---
name: suggest-clients
description: "Suggest clients: review past chats for clients or recurring projects, present up to five as cited guesses, and create nothing until the user picks one."
---

# Suggest clients from past chats

Use this when the user asks you to suggest clients, or when the Clients page's "Find clients in my past chats" starter prompt has been sent. The job is a short list of candidates with evidence, not a client record: nothing is saved until the user picks one.

## Reaching the tools

`session_search` is a stock Hermes tool and is usually in your tool list already; call it directly. The Engagements tools are `engagements_find`, `engagements_context`, `engagements_create` and `engagements_update`. On stock Hermes they are usually not in your tool list. Call `tool_search` with a query such as `client engagement`, then `tool_describe` for the schema, then `tool_call` with the exact tool name and its arguments. If a tool is listed directly, call it directly. If a call says the tool does not exist, go through `tool_call` before concluding it is missing.

Every Engagements result is JSON: `{ok, data?, error?, warnings}`. Read `ok` first. On `ok: false`, read `error.code` and `error.message`, tell the user in plain words, and do not retry blindly.

## Steps

1. **Search past chats, nothing else.** Use `session_search` with a few short queries drawn from the user's own words, and read the sessions it returns. Do not crawl the home folder, the filesystem or the library folder looking for client material, and do not read unrelated files on disk, unless the user names a folder and asks you to look there. Earlier chats are the evidence; your memory of them is not.
2. **Check what already exists.** For each candidate, call `engagements_find` with its name and any short form before you present it. When a candidate already has a record, say so next to it ("already in your library"), and do not propose it as new.
3. **Present up to five, as guesses.** One line per candidate with why it looks like a client or a recurring project, and which chats or sessions it came from, in the user's words where possible. Say plainly that these are guesses from old conversations and may be wrong, out of date or the same client under two names. If a candidate is really a project for a client you already listed, show it under that client.
4. **Never carry secrets across.** Do not copy passwords, API keys, tokens, card or bank numbers, private keys or any credential value into a suggestion. Name the client and the work; if a chat line holds a secret, leave the value out and say a secret was left out. Do not quote long passages from old chats either: a short phrase that identifies the work is enough.
5. **Create nothing until the user picks.** No `engagements_create`, no binding, no file written by any other means, and no record drafted and saved "to save time". Wait for the user to choose one candidate, or to say none of them.
6. **Hand off when they pick.** Once the user names a candidate, switch to the `onboard-client` skill: check duplicates with `engagements_find`, draft only from what the user and the cited chats say, put assumptions under `Unconfirmed` with their source, show the draft, apply corrections, and save only when they confirm. You may pre-fill the draft with the facts the cited chats support; label every one of them with its source.
7. **When nothing is found, onboard instead.** If the past chats hold nothing usable, or the user rejects the list, say so once and offer plain onboarding: "I did not find a client worth adding. Would you like to add one now?" Then follow the `onboard-client` skill.

## If a write warns

When a tool result carries a `possible_secret` warning, the record was saved with text that looks like a credential. Stop before any other action, tell the user what was saved and where (the result names the area, the section number and the line), and offer to remove the value. Never repeat the value in the conversation.

## Honest limits

A past-chat review reads the user's conversation history, so it can surface details about other clients. Keep the list short, keep it cited, and do not widen the search beyond what the request needs. Suggestions are not records: until the user confirms one, nothing about it is stored.
