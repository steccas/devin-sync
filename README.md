# devin-sync

Keep your **local** [Devin](https://devin.ai) Desktop and Devin CLI chats in sync between two or
more Linux PCs, through a folder you already sync with Nextcloud, Syncthing, Dropbox or similar.

> Unofficial. Not affiliated with or endorsed by Cognition. "Devin" and "Windsurf" are trademarks
> of their owners.

## Why

Devin syncs **cloud** sessions across devices on its own. **Local** sessions, the ones the agent
runs on your machine from Devin Desktop or the CLI, stay on the PC where you started them.

The obvious fix, symlinking Devin's data folder into Nextcloud, corrupts it. The sessions live in
SQLite databases in WAL mode, spread over three files that change together. A sync client uploads
them one at a time, and the other PC ends up with a database from one moment and a WAL from
another.

`devin-sync` wraps the Devin launcher instead:

1. **On start**, it merges the snapshot left by your other PC into your local data.
2. **On exit**, once every Devin process has closed, it writes a consistent snapshot back to the
   synced folder.

A lock file in the synced folder stops you from running Devin on two PCs at once and diverging.

## What gets synced

| Local data | How |
|---|---|
| `~/.local/share/devin/cli/sessions.db`: agent sessions, shared by Desktop and the CLI | Merged **per session**. A session that exists only on one PC is added; for a session present on both, the more recent `last_activity_at` wins |
| `windsurf.acp.*` rows of `~/.config/Devin/User/globalStorage/state.vscdb`: the chat list in the sidebar | Row by row. Only these rows are copied, never the whole file |
| `~/.config/Devin/User/acp-messages/*.db`: per-chat UI message store | Per file, newer wins |
| `~/.codeium/windsurf/{cascade,memories}`, `~/.windsurf/plans`: legacy Windsurf Cascade chats, memories, plans | Per file, newer wins |
| `~/.config/Devin/User/{settings.json,keybindings.json,snippets/}` | Per file, newer wins |

**Never synced:** the rest of `state.vscdb`, which holds your login token encrypted with this PC's
keyring. Also left alone are the Electron profile, caches and logs. Each PC keeps its own login.

## Requirements

- Linux with a freedesktop menu (KDE, GNOME, …). `notify-send` is optional and used for
  notifications.
- Python ≥ 3.10, standard library only.
- Devin Desktop installed (e.g. the AUR `devin-desktop` package) or the Devin CLI.
- A folder synced between your PCs.

Tested with Devin Desktop 3.10.35 and 3.10.48, CLI 3000.10.x, session schema v17.

## Install (on every PC)

```sh
git clone https://github.com/steccas/devin-sync.git ~/git/devin-sync
python3 ~/git/devin-sync/devin-sync install --sync-dir ~/Nextcloud/devin-sync
```

This does three things:
- writes `~/.config/devin-sync/config.json`;
- creates the `~/.local/bin/devin-sync` shim;
- adds a launcher override in `~/.local/share/applications/devin-desktop.desktop`, so opening
  Devin from the menu goes through the wrapper.

The `devin://` login URL handler is left untouched.

If you used Windsurf before, open Devin Desktop once normally **before** installing, so its
Windsurf → Devin migration can run.

## Usage

```sh
devin-sync desktop        # what the menu entry runs
devin-sync cli [args]     # Devin CLI, same pull/push around it
devin-sync sync           # pull + push right now (Devin must be closed)
devin-sync status         # lock, snapshot and local state
devin-sync --force desktop   # ignore a lock left by another PC
devin-sync uninstall
```

Each PC keeps a log at `~/.local/state/devin-sync/devin-sync.log`. Before every import,
devin-sync backs up your local data to `~/.local/state/devin-sync/backups/` and keeps the last 10.

## Good to know

- **Use one PC at a time.** If Devin is still open on the other PC, you get a notification and
  Devin does not start. After a crash, or a shutdown with Devin still open, the lock stays behind:
  use `--force`.
- **Keep repos at the same path on every PC.** A chat stores the absolute path of its working
  directory. If you clone `~/git/foo` on one PC and `~/src/foo` on the other, the chat arrives
  but points to a folder that doesn't exist.
- **Deletions are not propagated.** A chat you delete on one PC comes back from the other.
- **Different Devin versions.** If the session schema differs between PCs, the import is skipped
  and you are told. A PC with an older Devin never overwrites a snapshot written by a newer one.
- **Partial downloads.** `meta.json` stores the snapshot's SHA-256. If the sync client is still
  downloading `sessions.db`, the import waits, then skips, and retries on the next start.
- **Launching Devin another way** (straight from `/opt`, or from a terminal without the wrapper)
  skips syncing for that session. Nothing breaks: the next wrapped start or exit catches up.

## How the merge works

`sessions.db` rows are keyed by text session ids such as `laced-look`, and messages reference each
other by a per-session `node_id`. That makes a session the natural unit to merge.

For each session to import, devin-sync:
1. deletes the local copy of that session from every table that has a `session_id` column;
2. copies the remote rows back in, leaving out `AUTOINCREMENT` row ids so SQLite assigns fresh
   ones;
3. writes `sessions` first and the child tables after it.

Columns are matched by name, so small schema additions don't break the merge.

Snapshots are written with SQLite's backup API and stored in rollback-journal mode. Reading them
therefore never leaves `-wal`/`-shm` files in the synced folder.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The tests build synthetic Devin databases in temporary directories. No Devin install is needed.

## License

[MIT](LICENSE)
