# repo-scanner

GitHub Action that auto-generates descriptions and READMEs for your neglected repos using **Groq** or **Gemini** (both free tier).

## How it works

- Repos with a commit **within 30 days** → skipped (still active)
- Repos **older than 30 days** → scanned → description + README pushed
- Empty repos (zero commits) → skipped gracefully
- Large files (`.ipynb`, anything over 60 KB, minified files, lock files) → skipped
- Hard cap on total context sent to the model (default 50k chars)

## Setup

### 1. Fork / clone this repo into your GitHub account

### 2. Add secrets
Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|--------|-------|
| `SCANNER_GITHUB_TOKEN` | A PAT with `repo` scope — [create one here](https://github.com/settings/tokens) |
| `GROQ_API_KEY` | From https://console.groq.com |
| `GEMINI_API_KEY` | From https://aistudio.google.com (if using Gemini) |

> The default `GITHUB_TOKEN` in Actions only has access to the workflow repo. You need a PAT to read and write across all your other repos.

### 3. Edit `config.yaml`
Set your `username` and adjust limits if needed. Secrets stay out of the file.

### 4. Push — the workflow runs daily at 03:00 UTC

You can also trigger it manually: **Actions → Repo Scanner → Run workflow**

## Local run

```bash
pip install -r requirements.txt
export SCANNER_GITHUB_TOKEN=ghp_...
export GROQ_API_KEY=gsk_...
python main.py --config config.yaml
```

## Config

| Key | Default | What it does |
|-----|---------|-------------|
| `inactive_days` | 30 | Skip repos active within this window |
| `max_file_size_kb` | 60 | Skip files larger than this |
| `max_file_chars` | 2500 | Chars kept per file |
| `max_total_chars` | 50000 | Hard cap on total context sent to model |
| `max_files` | 40 | Max files collected per repo |
| `skip_forks` | true | Ignore forked repos |
| `overwrite_readme` | false | Overwrite existing READMEs |
| `dry_run` | false | Log changes without pushing |
