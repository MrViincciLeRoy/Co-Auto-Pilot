import argparse
import logging
from src.config import load_config
from src.scanner import run_scan

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M",
    level=logging.INFO,
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Log only, push nothing")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.dry_run:
        cfg.setdefault("scanner", {})["dry_run"] = True
    run_scan(cfg)
