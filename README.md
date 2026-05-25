# CAP — Co-Auto-Pilot

> Your repos, documented. Automatically.

CAP is a GitHub Action that watches your repositories and uses AI (Groq or Gemini) to write descriptions and READMEs for any repo that has been neglected — no manual effort needed. It runs daily, stays within free-tier API limits, and only touches repos that actually need help.

---

## How it works

CAP runs two passes on every cycle:

**Pass 1 — Inactive repos (30+ days without a commit)**
Scans all repos that haven't been touched recently. For each one missing a description, a README, or with a stub/empty README, CAP generates proper documentation and pushes it.

**Pass 2 — Recent repos (conditional)**
Only runs if Pass 1 found nothing to update. Sorts recently-active repos from oldest to newest and checks each one. Any repo with a missing description, missing README, or a stub README (just a title line or the repo name) gets rewritten — description and README both synced to match.

**Stub detection**
CAP detects READMEs that are technically present but useless — a single heading, just the repo name, or under 30 words that don't say anything meaningful. These get rewritten the same as empty ones.

---

## Setup

### 1. Fork or clone this repo into your GitHub account

### 2. Add secrets

Go to **Settings → Secrets and variables → Actions**:

| Secret | Where to get it |
|---|---|
| `SCANNER_GITHUB_TOKEN` | [github.com/settings/tokens](https://github.com/settings/tokens) — needs `repo` scope |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) |
| `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com) — only if using Gemini |

The default `GITHUB_TOKEN` only covers the workflow repo. You need a PAT to read and write across all your other repos.

### 3. Set your username in `config.yaml`

```yaml
github:
  username: "your_github_username"
```

### 4. Push — CAP runs daily at 03:00 UTC

Trigger manually anytime: **Actions → Repo Scanner → Run workflow**

---

## Local run

```bash
pip install -r requirements.txt

export SCANNER_GITHUB_TOKEN=ghp_...
export GROQ_API_KEY=gsk_...

python main.py --config config.yaml
```

---

## Configuration

```yaml
ai:
  provider: "groq"              # groq | gemini
  groq_model: "llama-3.3-70b-versatile"
  groq_rpm: 25                  # stay under the 30 RPM free-tier cap

scanner:
  inactive_days: 30             # repos active within this window are skipped in Pass 1
  max_file_size_kb: 60          # skip files larger than this
  max_file_chars: 2500          # chars kept per file
  max_total_chars: 50000        # hard cap on context sent to the model
  max_files: 40                 # max files collected per repo
  skip_forks: true              # ignore forked repos
  overwrite_readme: false       # true = overwrite existing non-stub READMEs too
  dry_run: false                # true = log everything, push nothing
```

---

## What gets skipped

- Repos with zero commits
- Forked repos (configurable)
- Repos with a real description **and** a non-stub README
- Files over the size limit, lock files, notebooks, minified assets, source maps

---

## Tech stack

- **Python 3.11**
- **PyGithub** — GitHub API
- **Groq** (`llama-3.3-70b-versatile`) or **Google Gemini** (`gemini-1.5-flash`)
- **PyYAML** — config parsing
- **GitHub Actions** — scheduling and secrets
