#!/usr/bin/env python3
"""Run the shared review-enrichment pipeline for the screenshot S/A tiers."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import analyze_incremental_reviews as analysis


analysis.GAMES = {
    4304930: "Chef Knight",
    3862670: "Shelldiver",
    3767740: "Outhold",
    3972320: "Loot Loop",
    3948120: "Scritchy Scratchy",
    4286550: "Keep on Mining! - Worlds",
    3833760: "You Know The Drill",
    3372980: "Tower Wizard",
    4305480: "IncreKnight",
}
analysis.DEFAULT_OUTPUT = Path("data/analysis/incremental-tier-list-2026-08-16")


if __name__ == "__main__":
    analysis.main()
