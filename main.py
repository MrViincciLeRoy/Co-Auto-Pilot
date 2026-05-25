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
    args = parser.parse_args()
    run_scan(load_config(args.config))
