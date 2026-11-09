# Editing the engine from your own computer

Hand-editing files in the GitHub website works, but it is slow and easy to get wrong.
This sets up a copy on your machine so changes are drag, save, click.

**Use GitHub Desktop, not the command line.** It does the same job with buttons, and
nothing here needs the terminal.

---

## One-time setup (about 10 minutes)

### 1. Install GitHub Desktop
Go to **desktop.github.com** → Download for Windows → run the installer.

### 2. Sign in
Open GitHub Desktop → **Sign in to GitHub.com** → it opens your browser → authorise.

### 3. Get your repository onto your computer
**File → Clone repository** → select `whitespace-engine` → choose where to put it
(the default `Documents\GitHub\whitespace-engine` is fine) → **Clone**.

You now have a real folder on your PC containing every file. This is your working copy.

---

## The everyday loop

Whenever you want to change something:

1. **Edit the file** in that folder. Use Notepad, VS Code, anything. Save normally.
2. **Open GitHub Desktop.** Your changes are listed on the left, with every edited line
   highlighted — green for added, red for removed. Read this; it is your safety net.
3. **Type a short summary** in the box at the bottom left, e.g. `widen ICP keywords`.
4. Click **Commit to main**.
5. Click **Push origin** at the top.

Within seconds GitHub has the change and the next scheduled run uses it.

That is the whole workflow. Edit, commit, push.

---

## Getting the engine's own updates

The engine writes back to the repo too — every run that finds something commits an
updated `master_tracker.csv`. Before you start editing, click **Fetch origin** then
**Pull origin** in GitHub Desktop. That pulls those rows down to your copy and avoids
a conflict.

Rule of thumb: **pull before you edit, push after you edit.**

---

## Testing a change before it goes live

You can run the engine on your own machine first. Worth doing before any change to the
gates, because a mistake there silently changes what reaches your inbox.

**One-time:**

1. Install Python from **python.org/downloads** — tick **"Add Python to PATH"** on the
   first screen of the installer. This matters.
2. Open the repo folder, click the address bar, type `cmd`, press Enter.
3. Run: `pip install -r requirements.txt`
4. Make a file called `.env` in that folder (copy `.env.example` and rename it), and
   fill in your keys. `.gitignore` already stops this file reaching GitHub — keep it
   that way.

**Then, to test:**

```
python -m src.run --once --dry-run
```

`--dry-run` sends no email and writes no tracker rows. It prints what it *would* have
sent. Use it freely.

To check the gate logic specifically:

```
pytest tests/
```

If those pass, your filters still behave as intended.

---

## What you will most likely want to change

| Want to change | File | Where |
| --- | --- | --- |
| Which sectors count as ICP | `config.yaml` | `gates.icp.accepted_verticals` |
| Which sectors are excluded | `config.yaml` | `gates.icp.disqualified_keywords` |
| Cities treated as India hubs | `config.yaml` | `gates.india.hub_cities` |
| News sources | `config.yaml` | `sources` |
| How often it runs | `.github/workflows/whitespace.yml` | the `cron` line |
| The email's tone and layout | `src/dispatch.py` | `render_digest_*` |
| How the AI judges and writes | `src/prompts.py` | `SYSTEM_PROMPT` |

`config.yaml` covers most of it, and it is plain text with comments. Start there.

---

## If something goes wrong

**You broke something and want to undo it.** In GitHub Desktop, right-click the file in
the changes list → **Discard changes**. It returns to the last committed version.

**You already pushed a bad change.** Go to your repo on GitHub → **Commits** → find the
last good one → open it → **Revert**. Nothing is ever truly lost.

**GitHub Desktop says there is a conflict.** You edited a file the engine also changed —
almost always `master_tracker.csv`. Easiest fix: discard your local change to that file
and pull again. You are not meant to hand-edit the tracker anyway.
