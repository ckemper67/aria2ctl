# aria2ctl

Command-line control for an aria2 daemon over JSON-RPC. Single-file Python 3
script, standard library only.

## Install

Symlink the script onto your PATH:

    ln -s ~/work/aria2ctl/aria2ctl ~/bin/aria2ctl

## Commands

| Command | Description |
|---|---|
| `list [--all]` | List torrent downloads (`--all` includes HTTP/FTP) |
| `status <gid>` | Detailed status, errors, and the follow-up GID for magnets |
| `files <gid>` | Show the files in a download |
| `select <gid> [--no-unpause]` | Interactively choose which torrent files to download |
| `add <file-or-link> [--start]` | Add a .torrent file, magnet link, or URL (paused by default; `upload` is an alias) |
| `pause <gid> \| --all [--force]` | Pause one or all downloads |
| `resume <gid> \| --all` | Resume one or all downloads |
| `remove <gid> [--delete-files] [-y] [--force]` | Remove a download, optionally deleting its files |
| `truncate <gid> [--apply]` | Truncate a partial HTTP/FTP download to its contiguous downloaded prefix |
| `clean` | Clear finished, failed, and removed downloads |

GIDs can be abbreviated to any unique prefix, like git commit hashes.

## Configuration

Settings come from, highest priority first: command-line flags, environment
variables, the config file, built-in defaults.

The config file is `~/.config/aria2/aria2ctl.env` (or under
`$XDG_CONFIG_HOME`). It holds `KEY=VALUE` lines; `#` comments, `export`, and
quotes are allowed. Keep it private: `chmod 600`.

| Variable | Flag | Default |
|---|---|---|
| `ARIA2_RPC_URL` | `--rpc-url` | `http://localhost:6800/jsonrpc` |
| `ARIA2_RPC_SECRET` | `--rpc-secret` | (none) |
| `ARIA2_LOCAL_DIR` | `--local-dir` | (none) |

## Remote daemons

aria2's RPC cannot delete or modify files, so `remove --delete-files` and
`truncate` operate on the filesystem of the machine running aria2ctl. When the
daemon is remote, set `ARIA2_LOCAL_DIR` to where aria2's download folder (its
global `dir` option) is mounted locally; server paths are translated to that
mount. Without it, these commands refuse to run against a remote daemon.

## Caveats

- `truncate` discards pieces completed after the first gap. aria2 still records
  them as done, so resuming the download afterwards leaves holes.
- Path translation assumes the daemon runs on a POSIX system.
