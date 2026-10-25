"""Read-only preference/compiler preview; no app-home, database, browser or API access.

python -m applypilot.discovery.preference_preview --searches searches.target.yaml
python -m applypilot.discovery.preference_preview --searches searches.target.yaml --facts examples.json
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import yaml
from .intent import compile_search_intent
from .preferences import ArrangementFacts, rank_opportunity


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--searches", type=Path, required=True)
    parser.add_argument("--facts", type=Path, help="JSON of ALREADY assessed synthetic/imported alternatives, not raw jobs")
    args = parser.parse_args(argv)
    try:
        intent = compile_search_intent(yaml.safe_load(args.searches.read_text(encoding="utf-8")))
        result = {
            "mode": "offline-preference-preview; no live discovery or application side effects",
            "intent_fingerprint": intent.fingerprint(),
            "queries_preserved": len(intent.queries), "queries_active": len(intent.active_queries()),
            "market_definitions": len(intent.markets), "active_markets": list(intent.active_market_labels),
            "sources_enabled": list(intent.sources_enabled), "jobspy_sites": list(intent.jobspy_sites),
            "preferences_in_order": [r.id for r in intent.preferences],
            "exploration": asdict(intent.exploration),
        }
        if args.facts:
            examples = json.loads(args.facts.read_text(encoding="utf-8"))
            if not isinstance(examples, list):
                raise ValueError("facts must be a list of {id, alternatives} objects")
            result["rankings"] = [{
                "id": item["id"],
                **asdict(rank_opportunity(intent.preferences,
                    [ArrangementFacts(**a) for a in item["alternatives"]], intent.active_market_labels)),
            } for item in examples]
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        print(f"preference-preview: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
