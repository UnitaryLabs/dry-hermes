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
  each space you belong to. Dry pushes every change the instant it happens. The same stream also feeds the agent's "what changed", with no
  polling for those spaces. The plugin sends a short message
  ("🔔 New in Dry …") through `hermes send` to the place you choose. That's plain text with no model call. A restart
  misses nothing: the stream resumes from the last change it saw.
- **Reacts on its own** (on by default, asked during setup). When **other people** change your spaces, Hermes starts a
  turn by itself in your chat with it on a messaging app (Telegram and the like). It tells you what changed and suggests a
  next step, or follows any standing instructions you gave it ("when a bug is filed, summarise it"). Guard rails: only
  other people's changes, never your own or its own; changes are batched; at most 6 reactions an hour, then a plain alert.
  `/dry watch react off` turns it off. It needs the gateway running and a chat you have had with Hermes there.
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
  - `/dry` in a chat: `find`, `find --all`, `save`, `changes [hours]`, `watch on <target> | off | status`, `watch react on | off`, `spaces`, `status`.
  - `hermes dry status | find | save | spaces` in the terminal.
- **Skills:**
  - `dry:using-dry` teaches the agent how to work in Dry: links, types, read-before-update, pages, recipes.
  - `dry:memories` explains what to save and what never to save.

Building in Dry (spaces, types, records, pages) goes through **Dry's MCP server**, which you connect separately (step 4).
This plugin adds the memory and the know-how on top. It does not duplicate those tools.

It is one plugin of two kinds (`kind: standalone`): a **memory provider** named `dry` plus a **general plugin** that
provides the command and the skills. It has no Python dependencies.

## Install — two commands

```bash
hermes plugins install UnitaryLabs/dry-hermes --enable
hermes memory setup dry
```

The second command walks you through it, and every question has a safe default (just press Enter):

1. **Your Dry address.** Defaults to `https://dry.ai`.
2. **Sign in in the browser.** A Dry page opens: sign in if asked and press **Approve and connect**. There is no token to
   copy. Hermes gets its own personal access token, named "Hermes (MCP connector)", which you can see and revoke in Dry
   under **Account → Agents & tokens**. It is saved in `~/.hermes/.env`, never in `config.yaml`.
3. **Dry's tools.** Answer yes (the default) and Hermes can also **create and work in any of your spaces**: spaces, types,
   records and pages. It uses the same sign-in. If you already connected Dry's MCP server yourself, setup leaves it as it is.
4. **Reacting on its own.** Yes by default: when other people change your spaces, Hermes tells you in your chat with it.
5. **Live alerts.** Choose where they go (`telegram`, `discord`, `signal`, or any `hermes send` target), or `none`.

It finishes with `Dry: signed in as you@… · memories: N in "Memories" · <link>`. Start a new chat. If you turned on
alerts, also run `hermes gateway restart`.

- **Hermes on a server, over SSH or in a container** (no browser on that machine): setup notices and shows a short code and
  an address instead:
  ```
  1. Open  https://dry.ai/device?code=WDJB-MJHT
     (or go to https://dry.ai/device and enter the code  WDJB-MJHT )
  2. Sign in to Dry if asked, check the code matches, and press "Approve and connect".
  ```
  Do that on any computer or phone; setup finishes by itself within a few seconds. It needs no input in the server's
  terminal, so it also works in `docker compose exec` without `-it`.
- **A Dry without device sign-in** (an older self-hosted one): setup asks you to open the link anywhere, approve, and paste
  back the address the browser lands on (`http://127.0.0.1:…/callback?code=…` — the page itself says it cannot connect,
  which is expected).
- **Prefer a token**: make one in Dry (**Account → Agents & tokens → Create**), then
  ```bash
  hermes config set DRY_TOKEN dry_pat_…     # saved in ~/.hermes/.env
  hermes memory setup dry                   # finds the token and skips the sign-in
  ```
  For Hermes in Docker run each as `docker compose exec -it <service> hermes …`, then `docker compose restart <service>`.
- The choice can be forced: `DRY_SIGNIN=device`, `DRY_SIGNIN=paste` or `DRY_SIGNIN=browser`.
- **Hermes Desktop:** the memory panel's **Connect** button runs the same browser sign-in.
- **Run it again any time** to reconnect, or to change the address or where alerts go.

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
| `react` | `true` | Hermes reacts on its own to other people's changes (gateway + a messaging chat) |
| `react_per_hour` | `6` | Most reactions per hour; past that, a plain alert |
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

- `tests/live_test.py` (phase 1, 28 checks), `tests/live_test_phase3.py` (reactions, the cap, the stream-fed digest) and
  `tests/live_test_phase2.py` (changes, the SSE watcher's live push and
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
