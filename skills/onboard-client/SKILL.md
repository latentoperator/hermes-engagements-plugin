---
name: onboard-client
description: "Add a client: draft a short record from the user's answers or pasted notes, correct it with them, then save it with engagements_create."
---

# Onboard a client

Use this when the user wants to add a client (or, afterwards, a project) to the Engagements library. The result is one short Markdown record that any agent working for that client can read later: who they are, who to talk to, how they like to work, and where the real documents and repos live.

## Reaching the tools

The tools are `engagements_find`, `engagements_create`, `engagements_context`, `engagements_update` and `engagements_bind`. On stock Hermes they are usually not in your tool list. Call `tool_search` with a query such as `client engagement`, then `tool_describe` for the schema, then `tool_call` with the exact tool name and its arguments. If a tool is listed directly, call it directly. If a call says the tool does not exist, go through `tool_call` before concluding it is missing.

Every result is JSON: `{ok, data?, error?, warnings}`. Read `ok` first. On `ok: false`, read `error.code` and `error.message`, tell the user in plain words, and do not retry blindly.

## Steps

1. **Check for duplicates first.** Call `engagements_find` with the client name, and again with any short form or alias the user mentions. If something close already exists, say so and ask whether to use it, update it, or create a separate record. Do not create over it.
2. **Draft only from what the user gave you.** Use their answers or their pasted notes. Do not invent contacts, emails, phone numbers, locations or facts. Leave a section empty rather than fill it with a guess.
3. **Separate confirmed facts from assumptions.** Put anything inferred, uncertain or stated second-hand under `## Unconfirmed`, with its source, in the form `- <the uncertain fact> (source: <where it came from>, assumed)`. Everything else goes in its normal section.
4. **Ask at most three follow-up questions in total**, and only ones the immediate work needs. When the user has given you little, ask for the essentials in one short message (what the client does, who the main contact is, which folder holds the work) and not a section-by-section questionnaire; draft from whatever comes back and leave the rest empty. If the user wants to move on, stop asking and save what you have.
5. **Show the draft and apply corrections before saving.** Present the record in readable form: name, summary, contacts, communication preferences, locations, systems of record, and the Unconfirmed list. Apply the user's corrections to the draft. Nothing is saved yet.
6. **Save with `engagements_create`.** Use `kind: "client"`, the `name`, optional `properties.aliases`, and the confirmed sections as `sections: [{"section": "Summary", "body": "..."}, ...]`. Use the section titles `Summary`, `Contacts`, `Communication preferences`, `Locations`, `Systems of record` and `Unconfirmed`. Set `source` to where the facts came from, for example `pasted kickoff notes` or `onboarding conversation`. Contacts are one line per person, parts separated by `|`: `- <name> | <role as the user stated it> | <email, only if given> | <note, only if given>`; leave out any part the user did not give.
7. **Handle `similar_exists`.** The error lists candidates. Show them to the user. Only retry with `allow_similar: true` if the user says this really is a different client.
8. **Confirm what was saved.** Report the id and the file path from the result. Then offer to add projects for this client.

## Projects

To add a project, run `engagements_find` with `kind: "project"` first, then call `engagements_create` with `kind: "project"`, the owning client's id in `client`, a `name`, and `properties.locations` for the folders, repos or document links the user names. Put the purpose in a `Purpose` section. Only absolute local folders take part in automatic binding; URLs are references. If two projects list the same folder, mention that a session started there will be ambiguous.

## Never store these

Record systems of record as references only: which system, which collection or customer name. Never store passwords, API keys, tokens, payment card numbers, bank details, private keys, or any secret value. If the user pastes one, leave it out, say you left it out, and record only where it lives (for example `Credentials: password manager collection "Acme"`).

## If a write warns

When a tool result carries a `possible_secret` warning, the record was saved with text that looks like a credential. Stop before any other action, tell the user what was saved and where (the result names the area, the section number and the line), and offer to remove the value. Never repeat the value in the conversation.

## Honest limits

A client record organizes context. It is not isolation: other clients' details may still be in earlier conversation or memory. Say so if the user asks about confidentiality, and suggest a fresh session per client.
