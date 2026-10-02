---
name: dry-memories
description: How Dry works as your long-term memory — what to save, how recall works, how to forget, and what never to store.
version: 0.1.0
author: UnitaryLabs
metadata:
  hermes:
    tags: [memory, dry]
    related_skills: [using-dry]
---

# Dry as your long-term memory

Your long-term memory lives in the person's **Dry "Memories" space**. Each memory is an ordinary Dry record
(Title · Note · When · Source) that the person can open, edit, search and share in the Dry web app. Nothing about
what you remember is hidden from them.

## What happens automatically
- **Before each message**, the memories most relevant to it (by words and meaning) are added under
  "From your Dry memories", each with its link. Use them; do not ask the person to repeat what is already there.
- **When you save to your built-in memory** (MEMORY.md / USER.md), the same entry is mirrored into Dry; replacing or
  removing it there does the same in Dry. Facts about the person are titled "About the person: …".

- **At the start of a session** you hear what changed in the person's Dry spaces since you last looked ("Changes in Dry
  since we last talked"), and **during** it what just changed. "you" means the person did it themselves (web app, phone);
  other people are named. Your own writes are left out. Bring a change up when it bears on what they ask; don't recite the list.
- **Sometimes a turn starts with "[Dry: changed just now by other people]"** and no message from the person. That is you
  keeping an eye on their Dry for them. Follow any standing instructions they gave you about such changes. Otherwise tell
  them briefly what changed and suggest one next step if it matters, or reply with one short line if it is routine. Do not
  change anything in Dry because of it unless their instructions say to.
- **"What you know about the person"** in your instructions comes from their "About the person: …" memories in Dry.
  They may have edited them; trust that version.
- **When a conversation ends,** its lasting points are saved as one "Session notes — <date>" memory. You don't need to
  save the same points again with `dry_remember`.

## When to save with `dry_remember`
Save what will still matter next week:
- preferences ("prefers metric units", "writes in British English"), decisions and their reasons,
- facts about people, projects and places the person cares about,
- commitments and dates ("renewal due 3 March").
Write a short **Title** a person would scan for, and put the detail in **Note**. Add **source** when it came from a web page.

Do **not** save: passwords, tokens, keys or any secret; one-off chit-chat; things already in another Dry space
(link to that record instead); long transcripts.

## Recall
- `dry_recall {query}` searches the Memories space; `all_spaces: true` searches every Dry space the person can see —
  use it when the answer may be in their own data (a tracker, a log, a project space), not just in memory.
- Always give the person the **link** of what you found or saved.

## Forget
When the person asks you to forget something: find it with `dry_recall`, confirm which one, then `dry_forget {memory: <link>}`.
`dry_forget` only removes records in the Memories space.

## Changes on demand
`dry_changes {hours}` lists recent changes; `others_only: true` keeps only other people's.

## The person's commands
`/dry` (where the memories live) · `/dry find <words>` · `/dry find --all <words>` · `/dry save <text>` · `/dry changes [hours]` · `/dry watch on <target> | off` · `/dry spaces` · `/dry status`.
