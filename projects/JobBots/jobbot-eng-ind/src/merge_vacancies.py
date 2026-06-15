from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return {"vacancies": []}
    return json.loads(text)


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge JobBot raw vacancy JSON files")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()

    vacancies: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in args.inputs:
        try:
            payload = load_json(path)
        except Exception as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
            continue

        items = payload.get("vacancies") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            errors.append(f"{path}: expected list or object with vacancies array")
            continue
        for item in items:
            if isinstance(item, dict):
                vacancies.append(item)
            else:
                errors.append(f"{path}: skipped non-object item")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"vacancies": vacancies}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Merged {len(vacancies)} vacancies from {len(args.inputs)} files")
    for error in errors:
        print(f"Warning: {error}")


if __name__ == "__main__":
    main()
