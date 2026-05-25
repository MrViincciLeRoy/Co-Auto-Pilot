# CAP — Co-Auto-Pilot

> Your repos, documented. Automatically.

CAP is a GitHub Action that scans all your repositories daily and uses AI to write descriptions and READMEs for anything that's been neglected. It runs on a schedule, respects free-tier API limits, rotates through multiple API keys, and kills itself cleanly if it runs out of quota — no hanging, no silent failures.

---

## How it works

CAP runs two passes on every cycle.

**Pass 1 — Inactive repos** scans every repo that hasn't had a commit in 30+ days. Any repo missing a description, missing a README, or with a stub README (just a title line or the repo name) gets generated and pushed.

**Pass 2 — Recent repos** only runs if Pass 1 found nothing to update. It sorts recently-active repos oldest-first and applies the same checks. This ensures recently-created repos don't get skipped forever just because they're active.

**Stub detection** catches READMEs that are technically present but useless — a single heading, just the repo name, or under 30 words that don't say anything meaningful.

---

## Setup

### 1. Fork or clone this repo into your GitHub account

### 2. Add secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | What it's for |
|---|---|
| `SCANNER_GITHUB_TOKEN` | PAT with `repo` scope — [create one here](https://github.com/settings/tokens) |
| `GROQ_API_KEY_1` | First Groq key — [console.groq.com](https://console.groq.com) |
| `GROQ_API_KEY_2` | Second key (optional, recommended) |
| `GROQ_API_KEY_3` | Third key (optional) |

The default `GITHUB_TOKEN` only covers the workflow's own repo — you need a PAT to read and write across all your other repos.

You can add as many Groq keys as you want (`GROQ_API_KEY_1`, `GROQ_API_KEY_2`, ...). CAP rotates through them automatically when rate limits are hit.

### 3. Set your username in `config.yaml`

```yaml
github:
  username: "your_github_username"
```

Leave it blank to default to the token owner.

### 4. Push — CAP runs daily at 03:00 UTC

Trigger it manually anytime: **Actions → Repo Scanner → Run workflow**

---

## Local run

```bash
pip install -r requirements.txt

export SCANNER_GITHUB_TOKEN=ghp_...
export GROQ_API_KEY_1=gsk_...

python main.py --config config.yaml
```

---

## Configuration

```yaml
ai:
  provider: "groq"                    # groq | gemini
  groq_model: "llama-3.3-70b-versatile"
  groq_rpm: 25                        # stay under the 30 RPM free-tier cap

scanner:
  inactive_days: 30                   # repos active within this window skip Pass 1
  max_file_size_kb: 60                # skip files larger than this
  max_file_chars: 2500                # chars kept per file
  max_total_chars: 14000              # hard cap on context sent to the model
  max_files: 40                       # max files collected per repo
  skip_forks: true                    # ignore forked repos
  overwrite_readme: false             # true = overwrite existing non-stub READMEs too
  dry_run: false                      # true = log everything, push nothing
```

---

## Rate limiting and key rotation

CAP manages Groq's free-tier limits automatically:

- Tracks token usage against the 12,000 TPM window and waits for it to reset before sending a request that would exceed it.
- Enforces a minimum gap between requests (configurable via `groq_rpm`).
- After a heavy request (5,000+ tokens), adds a proportional cooldown before the next one.
- On a 429, parses the retry-after time from the error message and waits that long plus a 5-second buffer — or a full 60 seconds if Groq doesn't say.
- Each key gets **3 rate-limit strikes** before it's marked dead. When a key dies, CAP rotates to the next live key.
- If **all keys are exhausted**, CAP logs a fatal error and stops the process immediately. It will not continue scanning repos with dead keys.
- If no keys are found at startup, CAP exits before doing anything.

---

## What gets skipped

- Repos with zero commits
- Forked repos (configurable)
- Repos that already have a real description and a non-stub README
- Files over the size limit, lock files, notebooks, minified assets, source maps

---

## Tech stack

- **Python 3.11**
- **PyGithub** — GitHub API
- **Groq** (`llama-3.3-70b-versatile`) or **Google Gemini** (`gemini-1.5-flash`)
- **PyYAML** — config parsing
- **GitHub Actions** — scheduling and secrets
