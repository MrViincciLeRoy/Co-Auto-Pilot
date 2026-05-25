import sys
import time
import logging
from datetime import datetime, timezone, timedelta
from github import Github

from src.groq_client import AIClient, AllKeysDead
from src.github_utils import is_inactive, get_readme_state, collect_files
from src.analyzer import analyze_repo, PayloadTooLargeError

log = logging.getLogger(__name__)


def process_repo(repo, ai: AIClient, cfg: dict, force_recent=False) -> bool:
    s = cfg.get("scanner", {})
    dry_run = s.get("dry_run", False)
    overwrite = s.get("overwrite_readme", False)
    inactive_days = s.get("inactive_days", 30)

    inactive, last_dt = is_inactive(repo, inactive_days)
    readme_state, readme_sha = get_readme_state(repo)

    if not force_recent:
        if inactive is None:
            log.info(f"  SKIP  {repo.name:<35}  empty repo")
            return False
        if not inactive:
            log.info(f"  SKIP  {repo.name:<35}  active ({last_dt.strftime('%Y-%m-%d')})")
            return False

    needs_desc = not repo.description or readme_state == "stub"
    needs_readme = overwrite or readme_state in ("missing", "empty", "stub")

    if not needs_desc and not needs_readme:
        label = "already documented (recent)" if force_recent else "already documented"
        log.info(f"  SKIP  {repo.name:<35}  {label}")
        return False

    reasons = []
    if not repo.description:
        reasons.append("no description")
    if readme_state in ("missing", "empty", "stub"):
        reasons.append(f"{readme_state} README")

    date_str = last_dt.strftime('%Y-%m-%d') if last_dt else "unknown"
    tag = "recent" if force_recent else f"last commit {date_str}"
    log.info(f"  SCAN  {repo.name}  ({tag}" + (f" · {', '.join(reasons)}" if reasons else "") + ")")

    files, total = collect_files(repo, s)
    count = len(files["priority"]) + len(files["normal"])

    if count == 0:
        log.info("        no processable code files — skipping")
        return False

    log.info(f"        {count} file(s) · {total:,} chars")

    try:
        result = analyze_repo(ai, repo.name, files)
    except AllKeysDead:
        raise
    except PayloadTooLargeError:
        log.error("        payload still too large after trimming — skipping")
        return False
    except Exception as e:
        log.error(f"        error: {e}")
        return False

    if not result:
        return False

    desc = result.get("description", "")[:255]
    readme = result.get("readme", "")

    if not desc and not readme:
        log.warning("        AI returned empty result — skipping")
        return False

    if dry_run:
        log.info(f"        [DRY RUN] desc   → {desc}")
        log.info(f"        [DRY RUN] readme → {readme[:120]}…")
        return True

    updated = False

    if needs_desc and desc:
        try:
            repo.edit(description=desc)
            log.info("        ✓ description set")
            updated = True
        except Exception as e:
            log.error(f"        description failed: {e}")

    if needs_readme and readme:
        try:
            if readme_state in ("empty", "stub") and readme_sha:
                msg = "docs: fill empty README" if readme_state == "empty" else "docs: rewrite stub README"
                repo.update_file("README.md", f"{msg} via repo-scanner", readme, readme_sha)
                log.info(f"        ✓ README {'filled' if readme_state == 'empty' else 'rewritten'}")
                updated = True
            elif readme_state == "missing":
                repo.create_file("README.md", "docs: add README via repo-scanner", readme)
                log.info("        ✓ README created")
                updated = True
            elif overwrite and readme_sha:
                repo.update_file("README.md", "docs: update README via repo-scanner", readme, readme_sha)
                log.info("        ✓ README updated")
                updated = True
        except Exception as e:
            log.error(f"        README failed: {e}")

    time.sleep(1.5)
    return updated


def _scan_repos(repos, ai, cfg, force_recent=False) -> int:
    updated = 0
    skip_forks = cfg.get("scanner", {}).get("skip_forks", True)

    for repo in repos:
        if skip_forks and repo.fork:
            log.info(f"  SKIP  {repo.name:<35}  forked")
            continue
        try:
            if process_repo(repo, ai, cfg, force_recent=force_recent):
                updated += 1
        except AllKeysDead as e:
            log.error("=" * 55)
            log.error(f"FATAL: {e}")
            log.error("Stopping scan — no live API keys remaining.")
            log.error("=" * 55)
            sys.exit(1)
        except Exception as e:
            log.error(f"  ERROR  {repo.name}: {e}")

    return updated


def run_scan(cfg: dict):
    log.info("=" * 55)
    log.info("Repo scanner — starting")

    gh_cfg = cfg.get("github", {})
    token = gh_cfg.get("token", "")
    if not token:
        raise SystemExit("No GitHub token — set SCANNER_GITHUB_TOKEN env var")

    from github import Auth
    gh = Github(auth=Auth.Token(token))
    ai = AIClient(cfg)

    username = gh_cfg.get("username", "").strip()
    user = gh.get_user(username) if (username and username != "your_github_username") else gh.get_user()

    repos = list(user.get_repos(type="owner"))
    skip_forks = cfg.get("scanner", {}).get("skip_forks", True)
    inactive_days = cfg.get("scanner", {}).get("inactive_days", 30)

    log.info(f"Repos: {len(repos)}  skip_forks={skip_forks}")

    log.info("-" * 55)
    log.info("Pass 1 — inactive repos (30+ days)")
    inactive_updated = _scan_repos(repos, ai, cfg, force_recent=False)
    log.info(f"Pass 1 done — {inactive_updated} repo(s) updated")

    if inactive_updated > 0:
        log.info("-" * 55)
        log.info(f"Pass 2 — SKIPPED (Pass 1 updated {inactive_updated} repo(s))")
        log.info("Scan complete.")
        return

    log.info("-" * 55)
    log.info("Pass 2 — recent repos (oldest-first)")

    cutoff = datetime.now(timezone.utc) - timedelta(days=inactive_days)
    recent = []
    for repo in repos:
        if skip_forks and repo.fork:
            continue
        try:
            _, last_dt = is_inactive(repo, inactive_days)
            if last_dt and last_dt >= cutoff:
                recent.append((last_dt, repo))
        except Exception as e:
            log.error(f"  ERROR  {repo.name}: {e}")

    recent.sort(key=lambda x: x[0])
    log.info(f"Recent repos to check: {len(recent)}")

    recent_updated = _scan_repos([r for _, r in recent], ai, cfg, force_recent=True)
    log.info(f"Pass 2 done — {recent_updated} repo(s) updated")
    log.info("Scan complete.")
