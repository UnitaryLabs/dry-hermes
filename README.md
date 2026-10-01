# Dry for Hermes Agent

Dry becomes [Hermes Agent](https://hermes-agent.nousresearch.com)'s long-term memory. Hermes also learns how to use Dry well.

- **Recall before every message.** Before each turn, the memories relevant to it are found in your Dry **Memories** space
  (by words and meaning) and added to the message, each with its link.
- **Nothing hidden.** Whatever Hermes saves to its own memory is mirrored into Dry as an ordinary record. That covers
  adding, replacing and removing; facts about you are titled "About the person: …". You can see, edit, search, share
  and export all of it in the Dry web app.
- **Aware of what changed.** At the start of a session the agent hears what changed in your Dry spaces since it last
  looked: **you** in the web app or on your phone, or **other people** (by name), each with a link. During a session it
  hears what just changed. Its own writes are never reported back to it, including writes through Dry's MCP tools.
- **Live alerts** (`/dry watch on telegram`). While the Hermes **gateway** runs, the plugin holds a live stream (SSE) to
  each space you belong to. Dry pushes every change the instant it happens, and the plugin sends a short message
  ("🔔 New in Dry …") through `hermes send` to the place you choose. That's plain text with no model call. A restart
  misses nothing: the stream resumes from the last change it saw.
- **Session notes.** When a conversation ends, or before Hermes compresses a long one, its lasting points (decisions,
  dates, preferences, commitments) are saved as **one** memory for that session, written by your own active model.
  Short chats and small talk are skipped.
- **What you know about me.** Every "About the person: …" memory is put in front of the agent at the start of each
  session. Edit one in Dry and the agent knows the new version next time.
- **Tools for the agent:**
  - `dry_remember` saves a memory.
  - `dry_recall` searches your memories, or every Dry space you can see.
  - `dry_changes` lists recent changes on demand; `others_only` limits it to other people's.
  - `dry_forget` removes a memory, and only a memory.
- **For you:**
  - `/dry` in a chat: `find`, `find --all`, `save`, `changes [hours]`, `watch on <target> | off | status`, `spaces`, `status`.
  - `hermes dry status | find | save | spaces` in the terminal.
- **Skills:**
  - `dry:using-dry` teaches the agent how to work in Dry: links, types, read-before-update, pages, recipes.
  - `dry:memories` explains what to save and what never to save.

Building in Dry (spaces, types, records, pages) goes through **Dry's MCP server**, which you connect separately (step 4).
This plugin adds the memory and the know-how on top. It does not duplicate those tools.

It is one plugin of two kinds (`kind: standalone`): a **memory provider** named `dry` plus a **general plugin** that
provides the command and the skills. It has no Python dependencies.

## Install

1. **Get a Dry token.** In Dry, click the account icon (top right), then **Account** → **Agents & tokens** →
   **Personal access tokens**. Name it (for example "Hermes"), then **Create**. Copy the token (it starts with
   `dry_pat_`); it is shown only once.
2. **Put the plugin in place:**
   ```bash
   git clone https://github.com/UnitaryLabs/dry-hermes ~/.hermes/plugins/dry
   hermes plugins enable dry
   ```
3. **Make Dry the memory provider and add the token:**
   - Add these lines to `~/.hermes/config.yaml`:
     ```yaml
     memory:
       provider: dry
     ```
   - Add this line to `~/.hermes/.env`:
     ```
     DRY_TOKEN=dry_pat_…
     ```
   - If your Dry is not `https://dry.ai`, also add `DRY_URL=https://your-dry-address` to `~/.hermes/.env`.
   - Instead of editing the files, you can run `hermes memory setup` and choose **dry**; it asks for the same token.
4. **Connect Dry's tools, if you haven't already.** Run this in a regular terminal, not through another program's
   prompt. A browser opens: sign in to Dry and press **Approve and connect**.
   ```bash
   hermes mcp add dry --url https://dry.ai/api/mcp --auth oauth --connect-timeout 300
   ```
5. **Check it,** then start a **new** chat:
   ```bash
   hermes dry status    # Dry: signed in as you@… on https://dry.ai · memories: N in "Memories" · <link>
   ```

Your memories live in the space you own called **Memories**, which every Dry account gets. If yours is missing, the
plugin creates it, with the same Memory type (Title · Note · When · Source). To use a different space, put its name or
id in `~/.hermes/dry.json` as `{"space": "…"}`.

## Settings (`~/.hermes/dry.json`, all optional)

| Key | Default | Meaning |
|---|---|---|
| `watch` | `"all"` | Which spaces to follow for changes: every space you belong to (up to 20), or a list of names or ids |
| `notify_to` | `""` | Where live alerts go: `telegram` (home channel), `discord:#ops`, `signal:+1…`, any `hermes send` target. Empty means off |
| `notify_own` | `false` | Also alert about your own edits |
| `changes` | `true` | Tell the agent what changed since it last looked |
| `session_notes` | `true` | Save each conversation's lasting points |
| `space` | `"Memories"` | The space used as memory |

`/dry watch on <target>` and `/dry watch off` set `notify_to` for you. Alerts start with the gateway (`hermes gateway restart`).

## What it does not do

- **It does not store conversations.** Your Memories space is for things worth remembering, not chat logs.
- **It does not mirror subagent or scheduled-job memory.** Memory written by subagents and scheduled jobs (`agent_context`
  other than `primary`) is not mirrored.
- **It never stores secrets,** and the skills tell the agent the same.
- **It never deletes outside Memories.** `dry_forget` refuses a record outside the Memories space. The mirror removes
  only records it created itself, tracked in `dry-memory-map.json` in your Hermes home.

## Remove it

- **Stop using it:** `hermes plugins disable dry`, then set `memory.provider` back to what you had. Your memories stay in Dry.
- **Revoke its access:** in Dry, **Account** → **Agents & tokens**, revoke the token.

## Development

- `tests/live_test.py` (phase 1, 28 checks) and `tests/live_test_phase2.py` (changes, the SSE watcher's live push and
  replay, session notes on the real model, profile, `/dry watch`) run the plugin through Hermes's **real** loaders against a
  running Dry. Phase 1 covers:
  - discovery: the general loader, the memory loader, the command, the skills;
  - recall: tool routing, the prefetch block, the trivial-prompt skip;
  - mirroring: add, replace, remove, the user-fact title, subagents skipped;
  - `/dry`; forget and its guard;
  - a bad token returning an error rather than raising;
  - cleanup.

  Run it with a **throwaway** `HERMES_HOME`, never your real one. The test refuses to run against `~/.hermes`.
- `hermes plugins doctor <path> --ci` and `hermes plugins validate <path>` must pass. `validate` includes Hermes's
  security scan.
