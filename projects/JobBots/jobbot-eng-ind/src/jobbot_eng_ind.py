from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_OUTPUT_ROOT = Path(r"D:\Job Search 2026")
DEFAULT_STATE_FILE = DEFAULT_OUTPUT_ROOT / "seen-vacancies.json"
DEFAULT_DRIVE_PATH = ["Projects", "JobBots", "jobbot-eng-ind"]
BERLIN_TIMEZONE = ZoneInfo("Europe/Berlin")
REPORT_SECTIONS = ["Data Engineering", "AI Engineering", "Oracle / Enterprise Data"]
ORACLE_ENTERPRISE_TERMS = [
    "oracle",
    "pl/sql",
    "oci",
    "oracle ebs",
    "oracle erp",
    "oracle odi",
    "goldengate",
    "oracle apex",
    "data migration",
    "data replication",
    "erp",
    "wms",
    "supply chain",
    "logistics",
]
AI_ENGINEERING_TERMS = [
    "ai engineer",
    "artificial intelligence engineer",
    "generative ai",
    "genai",
    "llm",
    "large language model",
    "retrieval augmented generation",
    "rag",
    "mlops",
    "machine learning engineer",
    "ai platform",
    "azure ai",
    "azure openai",
    "langchain",
    "vector database",
    "embedding",
    "model serving",
]


def _canonical_text(value: Any) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"\b(?:all genders|m\s*/\s*[fw]\s*/\s*(?:d|\*)|[fw]\s*/\s*m\s*/\s*d|d\s*/\s*f\s*/\s*m|w\s*/\s*m\s*/\s*x|gn)(?![a-z0-9])", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def vacancy_key(vacancy: dict[str, Any]) -> str:
    """Return a stable identity for one role across job-board mirrors."""
    return "v2|" + "|".join(
        [
            _canonical_text(vacancy.get("company", "")),
            _canonical_text(vacancy.get("title", "")),
        ]
    )


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    text = path.read_text(encoding="utf-8-sig").strip()
    return json.loads(text) if text else fallback


def load_state(path: Path) -> dict[str, Any]:
    state = load_json(path, {})
    if not isinstance(state, dict):
        raise ValueError(f"State file must contain a JSON object: {path}")
    return state


def known_vacancy_keys(state: dict[str, Any]) -> set[str]:
    keys = set(state)
    for item in state.values():
        if isinstance(item, dict):
            keys.add(vacancy_key(item))
    return keys


def add_new_items_to_state(
    state: dict[str, Any], vacancies: list[dict[str, Any]], now: datetime
) -> None:
    for vacancy in vacancies:
        key = vacancy_key(vacancy)
        state[key] = {
            "company": vacancy.get("company"),
            "title": vacancy.get("title"),
            "location": vacancy.get("location"),
            "url": vacancy.get("url"),
            "first_seen": now.date().isoformat(),
            "run": "JobBot Eng Ind",
            "language_priority": vacancy.get("priority"),
        }


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as temporary_file:
        temporary_file.write(encoded)
        temporary_file.write("\n")
        temporary_path = Path(temporary_file.name)
    os.replace(temporary_path, path)


def contains_category_term(text: str, term: str) -> bool:
    if len(term) <= 3:
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    return term in text


def vacancy_category(vacancy: dict[str, Any]) -> str:
    text = " ".join(
        str(vacancy.get(field, ""))
        for field in ["title", "core_tech_match", "analysis", "recommendation"]
    ).casefold()
    if any(contains_category_term(text, term) for term in ORACLE_ENTERPRISE_TERMS):
        return "Oracle / Enterprise Data"
    if any(contains_category_term(text, term) for term in AI_ENGINEERING_TERMS):
        return "AI Engineering"
    return "Data Engineering"


def append_grouped_vacancies(
    lines: list[str], vacancies: list[dict[str, Any]], status: str, start_index: int
) -> int:
    index = start_index
    by_section = {section: [] for section in REPORT_SECTIONS}
    for vacancy in vacancies:
        by_section[vacancy_category(vacancy)].append(vacancy)

    for section in REPORT_SECTIONS:
        lines.extend([section, ""])
        section_vacancies = by_section[section]
        if not section_vacancies:
            lines.extend(["- No matches in this category.", ""])
            continue
        for vacancy in section_vacancies:
            lines.extend(format_vacancy(index, vacancy, status))
            index += 1
    return index


def render_report(
    vacancies: list[dict[str, Any]],
    state: dict[str, Any],
    now: datetime,
    *,
    new_only: bool = False,
) -> tuple[str, list[dict], list[dict]]:
    new_items: list[dict[str, Any]] = []
    watch_items: list[dict[str, Any]] = []
    known_keys = known_vacancy_keys(state)
    for vacancy in vacancies:
        if vacancy_key(vacancy) in known_keys:
            watch_items.append(vacancy)
        else:
            new_items.append(vacancy)

    lines = [
        "JobBot Eng Ind - English indexed job-search report",
        f"Generated: {now.strftime('%d.%m.%Y %H:%M')}",
        "",
        "Focus:",
        "- English-friendly Data Engineering, AI Engineering, and Oracle/Enterprise Data roles.",
        "- Manual web/indexed-search approach.",
        "- NRW local/hybrid roles are mixed with Germany remote roles.",
        "- German-heavy or non-Germany roles are marked lower priority.",
        "- No tailored CVs were generated.",
        "",
        f"New matches in this run: {len(new_items)}",
        f"Previously seen / watchlist repeated: {len(watch_items)}",
        "",
        "New / urgent matches",
        "",
    ]
    next_index = append_grouped_vacancies(lines, new_items, "NEW", 1)

    if not new_only:
        lines.extend(["", "Still relevant / watchlist", ""])
        append_grouped_vacancies(lines, watch_items, "previously seen", next_index)

    lines.extend(
        [
            "",
            "Source coverage",
            "- Manual indexed sources checked during this run; see each vacancy source line.",
            "- CV generation: disabled. Generate tailored CVs manually only for selected vacancy numbers.",
        ]
    )
    return "\n".join(lines), new_items, watch_items


def format_vacancy(index: int, vacancy: dict[str, Any], status: str) -> list[str]:
    return [
        f"{index}. [{vacancy.get('priority', 'n/a')}] {vacancy.get('company', 'Unknown')} - {vacancy.get('title', 'Untitled')}",
        f"   Status: {status}",
        f"   Location/work mode: {vacancy.get('location', 'not specified')} | {vacancy.get('work_mode', 'not specified')}",
        f"   Salary: {vacancy.get('salary', 'not specified')}",
        f"   Source: {vacancy.get('source', 'not specified')}",
        f"   Link: {vacancy.get('url', '')}",
        f"   Language: {vacancy.get('language', 'not specified')}",
        f"   Language risk: {vacancy.get('language_risk', 'not specified')}",
        f"   Contract type: {vacancy.get('contract_type', 'not specified')}",
        f"   Core tech match: {vacancy.get('core_tech_match', 'not specified')}",
        f"   Fit score: {vacancy.get('fit_score', 'not specified')}",
        f"   Analysis: {vacancy.get('analysis', '')}",
        f"   Recommendation: {vacancy.get('recommendation', '')}",
        "",
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="JobBot Eng Ind report generator")
    parser.add_argument("--vacancies-json", required=True, type=Path, help="JSON file with vacancy objects")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--drive", action="store_true", help="Write reports and state to Google Drive")
    parser.add_argument("--new-only", action="store_true", help="Only include new vacancies in the report body")
    parser.add_argument(
        "--commit-state",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Record newly reported vacancies in state (default: true)",
    )
    parser.add_argument(
        "--pending-vacancies-file",
        type=Path,
        help="Write the vacancies that would be newly reported for later state commit",
    )
    parser.add_argument(
        "--commit-only",
        action="store_true",
        help="Record --vacancies-json in state without rendering a report",
    )
    parser.add_argument("--google-client-file", type=Path, default=Path("credentials/google-oauth-client.json"))
    parser.add_argument("--google-token-file", type=Path, default=Path("credentials/google-token.json"))
    args = parser.parse_args()

    now = datetime.now(BERLIN_TIMEZONE)
    vacancies = load_json(args.vacancies_json, [])
    if not isinstance(vacancies, list):
        raise ValueError("Vacancies JSON must contain a JSON array")
    if args.drive:
        from drive_store import build_drive_service, download_json_file, ensure_path, upload_text_file

        drive = build_drive_service(args.google_client_file, args.google_token_file)
        project_folder = ensure_path(drive, DEFAULT_DRIVE_PATH)
        state_folder = ensure_path(drive, [*DEFAULT_DRIVE_PATH, "state"])
        runs_folder = ensure_path(drive, [*DEFAULT_DRIVE_PATH, "runs"])
        run_folder = None
        if not args.commit_only:
            run_folder = ensure_path(drive, [*DEFAULT_DRIVE_PATH, "runs", now.strftime("%Y-%m-%d")])
        ensure_path(drive, [*DEFAULT_DRIVE_PATH, "resumes"])
        state = download_json_file(drive, state_folder["id"], "seen-vacancies.json", {})
        if not isinstance(state, dict):
            raise ValueError("Drive state file must contain a JSON object")
    else:
        run_dir = args.output_root / f"{now.strftime('%d.%m.%Y')} JobBot Eng Ind"
        state = load_state(args.state_file)

    if args.commit_only:
        add_new_items_to_state(state, vacancies, now)
        if args.drive:
            upload_text_file(
                drive,
                state_folder["id"],
                "seen-vacancies.json",
                json.dumps(state, ensure_ascii=False, indent=2),
                mime_type="application/json",
            )
        else:
            write_json_atomic(args.state_file, state)
        print(f"Committed {len(vacancies)} vacancies to state")
        return

    if not args.drive:
        run_dir.mkdir(parents=True, exist_ok=True)
    report, new_items, _ = render_report(vacancies, state, now, new_only=args.new_only)

    if args.pending_vacancies_file:
        write_json_atomic(args.pending_vacancies_file, new_items)
    if args.commit_state:
        add_new_items_to_state(state, new_items, now)

    state_text = json.dumps(state, ensure_ascii=False, indent=2)
    vacancies_text = json.dumps(vacancies, ensure_ascii=False, indent=2)
    if args.drive:
        assert run_folder is not None
        upload_text_file(drive, run_folder["id"], "email-report.txt", report)
        upload_text_file(drive, run_folder["id"], "vacancies.json", vacancies_text, mime_type="application/json")
        if args.commit_state:
            upload_text_file(drive, state_folder["id"], "seen-vacancies.json", state_text, mime_type="application/json")
        print(f"Drive project folder: {project_folder.get('webViewLink')}")
        print(f"Drive runs folder: {runs_folder.get('webViewLink')}")
        print(f"Drive run folder: {run_folder.get('webViewLink')}")
    else:
        (run_dir / "email-report.txt").write_text(report, encoding="utf-8")
        (run_dir / "vacancies.json").write_text(vacancies_text, encoding="utf-8")
        if args.commit_state:
            write_json_atomic(args.state_file, state)
        print(run_dir / "email-report.txt")
    print(f"Wrote {len(vacancies)} vacancies ({len(new_items)} new)")


if __name__ == "__main__":
    main()
