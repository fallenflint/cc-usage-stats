# cc-usage-stats

Local spend statistics for Claude Code, computed only from the transcripts on your disk
(`~/.claude/projects`, plus WSL distros that are already running). No API key, no network.

- **Collector** — `ccstats.py collect`: reads new lines of transcript JSONL (subagents included)
  into SQLite incrementally, deduplicates usage by message id and prices it at API list rates.
- **Dashboard** — `ccstats.py serve` → http://127.0.0.1:8777: today / subscription month / all time,
  daily chart by model, projects, models, sessions, a "Refresh" button.
- **Tray icon** (Windows) — tooltip shows today's and this month's spend; menu: open dashboard,
  collect now, data folder, exit. Starts the dashboard server with it.
- **Status line in Claude Code** — this repository is also a plugin: `сегодня $X · месяц $Y`
  and `/usage-stats` for the dashboard link.

Costs are API-equivalent estimates (what the same tokens would cost on the API), not your bill.

## Install (Windows)

Needs Python 3.10+ (`py` launcher or `pythonw` on PATH). No admin rights.

```powershell
git clone <this repo> F:\Development\cc-usage-stats
powershell -ExecutionPolicy Bypass -File F:\Development\cc-usage-stats\install.ps1
```

`install.ps1 [-Hours 6] [-NoCollect]`:

- creates the data folder `~/.claude/usage-stats` with `config.json` (from `config.example.json`, kept if present);
- adds the Task Scheduler job `ClaudeUsage\Collect`, every `-Hours` hours (default 6);
- adds a "Claude Usage" shortcut to Startup (tray + dashboard at logon);
- runs the first full collect (a few minutes for many GB of transcripts) and starts the tray.

Then, in Claude Code:

```
/plugin marketplace add F:\Development\cc-usage-stats
/plugin install cc-usage-stats@cc-usage-stats
```

## Configure

Edit `~/.claude/usage-stats/config.json` (personal, never in the repo):

| key | meaning |
| --- | --- |
| `billing_day` | day of month your subscription renews; "month" in the stats starts on it |
| `tz` | hours from UTC used to cut days; omit to use this PC's zone |
| `port` | dashboard port (default 8777) |
| `rules` | `[regex, project name]` pairs matched against the lowercased cwd with `/`; first match wins, unmatched folders are named by their last path segment |
| `extra_roots` | more folders that contain Claude Code `projects/` transcripts |

Set `CCSTATS_DIR` to keep data somewhere else. After changing rules run `ccstats.py rebuild`.

## Commands

```
py -3 ccstats.py collect [-q]   # incremental collect (~1 s when little changed)
py -3 ccstats.py serve [port]   # dashboard
py -3 ccstats.py status         # totals in the console
py -3 ccstats.py rebuild        # drop the database and re-read everything
```

## WSL

The collector never starts WSL: it asks `wsl.exe -l --running -q` and reads
`\\wsl.localhost\<distro>\home\*\.claude\projects` only for distros that are already running.
A stopped distro is picked up on the next collect after it has been started.

Claude Code deletes old transcripts (`cleanupPeriodDays`, 30 days by default); collected
history stays in the database, so keep the schedule shorter than that.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-Purge]
```

Removes the scheduled job and Startup shortcut and stops tray and server; `-Purge` also deletes
the data folder. In Claude Code: `/plugin uninstall cc-usage-stats`.

## License

MIT
