---
name: using-dry
description: How to work in Dry well through its MCP tools — spaces, types, records, pages, files and links — and the habits that make an agent good at it.
version: 0.1.2
author: UnitaryLabs
metadata:
  hermes:
    tags: [dry, mcp, database]
    related_skills: [dry:memories]
---

# Working in Dry

**Dry** is the person's shared object database. **Spaces** hold **types** (fields with permanent ids; labels work when
unambiguous), **records** of those types, and **pages** — live views written as HTML/CSS/JS from a plain-language prompt.
Everything is reached through **Dry's MCP server**. In Hermes its tools are named `mcp__dry__<tool>` —
`mcp__dry__list_spaces`, `mcp__dry__list_types`, `mcp__dry__create_type`, `mcp__dry__list_objects`,
`mcp__dry__search_objects`, `mcp__dry__get_object`, `mcp__dry__create_object`, `mcp__dry__update_object`,
`mcp__dry__edit_page`, `mcp__dry__export_space`, … — call them directly; any other name for Dry's tools is wrong.
If those tools are missing, Dry was connected after this chat started, or not at all; tell the person:
start a new chat, and if they are still missing run `hermes memory setup dry` (signs in to Dry in the browser).

## Triggers
"Hey Dry", "remember …", "save …", "store …", "track …", "keep track of …", "log …", "add … to Dry", and questions about
something they saved before — all are Dry requests.

## Habits of a good Dry agent
1. **Always hand over the link.** Every space, record and page a tool returns carries a `url`. After creating, changing or
   finding something, give the person that link.
2. **Look before you write.** `list_spaces` → `list_types` in the right space → then create. A new kind of data that fits
   no type gets a **new type** (`create_type`), not a new space; create a space only when asked for a separate place.
3. **Read before you update.** Update only a record you read in this turn (`get_object` / `list_objects`), then read it
   back after the write and check it says what you meant.
4. **Answer questions from the records.** "How many…", "what did I…", "show me…" → list or search the records and count
   or summarise them. Never say the tools cannot list or count.
5. **Pages:** to make one, `create_object` with the built-in Page type and a plain-language **Prompt**; the page fills in
   within about a minute — give the link straight away. To change one, `edit_page` with what to change; the page keeps
   everything asked for before.
6. **Deletes are confirmed** with the person first.
7. **Exports** (`export_space`) are only for backing up or moving a whole space; never use them to look something up.
8. **Times:** call `get_me` for the person's time zone and the current time before writing dates.
9. **Problems with Dry itself:** if a Dry tool fails in a way you cannot fix by correcting your call, answers something wrong or
   confusing, or lacks what the person needed, call `mcp__dry__report_issue` (what you tried · what happened · what you expected)
   and tell the person you reported it. It reaches the people who build Dry; never use it for the person's own data.

## Recipes
- **Track something new** ("track my workouts"): find or create a space → `create_type` Workout with fields
  (Date · Activity · Duration · Notes) → `create_object` the first entry (fields by label; on a space's own endpoint the same tool is `create_workout`) → link.
- **Log into an existing tracker** ("log 5 km this morning"): `list_types` → the matching type → create one record → link.
- **Build a view** ("a page of this month's expenses by category"): `create_object` Page with that prompt → link.
- **Remember about the person**: use `dry_remember` (your memory), not a new space.

See also `dry:memories` for how your own memory works in Dry.
