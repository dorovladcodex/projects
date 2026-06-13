from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT_ROOT = Path(r"D:\Job Search 2026")
DEFAULT_STATE_FILE = DEFAULT_OUTPUT_ROOT / "seen-vacancies.json"
DEFAULT_DRIVE_PATH = ["Projects", "JobBots", "jobbot-eng-ind"]


def vacancy_key(vacancy: dict[str, Any]) -> str:
    parts = [
        vacancy.get("company", ""),
        vacancy.get("title", ""),
        vacancy.get("location", ""),
        vacancy.get("url", ""),
    ]
    return "|".join(str(part).strip().lower() for part in parts)


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else fallback


def render_report(vacancies: list[dict[str, Any]], state: dict[str, Any], now: datetime) -> tuple[str, list[dict], list[dict]]:
    new_items: list[dict[str, Any]] = []
    watch_items: list[dict[str, Any]] = []
    for vacancy in vacancies:
        if vacancy_key(vacancy) in state:
            watch_items.append(vacancy)
        else:
            new_items.append(vacancy)

    lines = [
        "JobBot Eng Ind - English indexed job-search report",
        f"Generated: {now.strftime('%d.%m.%Y %H:%M')}",
        "",
        "Focus:",
        "- English-friendly Senior Data Engineer / Azure Databricks / Data Platform roles.",
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

    for index, vacancy in enumerate(new_items, 1):
        lines.extend(format_vacancy(index, vacancy, "NEW"))

    lines.extend(["", "Still relevant / watchlist", ""])
    for index, vacancy in enumerate(watch_items, len(new_items) + 1):
        lines.extend(format_vacancy(index, vacancy, "previously seen"))

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
        f"   Analysis: {vacancy.get('analysis', '')}",
        "",
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="JobBot Eng Ind report generator")
    parser.add_argument("--vacancies-json", required=True, type=Path, help="JSON file with vacancy objects")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--drive", action="store_true", help="Write reports and state to Google Drive")
    parser.add_argument("--google-client-file", type=Path, default=Path("credentials/google-oauth-client.json"))
    parser.add_argument("--google-token-file", type=Path, default=Path("credentials/google-token.json"))
    args = parser.parse_args()

    now = datetime.now()
    vacancies = load_json(args.vacancies_json, [])
    if args.drive:
        from drive_store import build_drive_service, download_json_file, ensure_path, upload_text_file

        drive = build_drive_service(args.google_client_file, args.google_token_file)
        project_folder = ensure_path(drive, DEFAULT_DRIVE_PATH)
        state_folder = ensure_path(drive, [*DEFAULT_DRIVE_PATH, "state"])
        runs_folder = ensure_path(drive, [*DEFAULT_DRIVE_PATH, "runs"])
        run_folder = ensure_path(drive, [*DEFAULT_DRIVE_PATH, "runs", now.strftime("%Y-%m-%d")])
        ensure_path(drive, [*DEFAULT_DRIVE_PATH, "resumes"])
        state = download_json_file(drive, state_folder["id"], "seen-vacancies.json", {})
    else:
        run_dir = args.output_root / f"{now.strftime('%d.%m.%Y')} JobBot Eng Ind"
        run_dir.mkdir(parents=True, exist_ok=True)
        state = load_json(args.state_file, {})
    report, new_items, _ = render_report(vacancies, state, now)

    for vacancy in new_items:
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

    state_text = json.dumps(state, ensure_ascii=False, indent=2)
    vacancies_text = json.dumps(vacancies, ensure_ascii=False, indent=2)
    if args.drive:
        upload_text_file(drive, run_folder["id"], "email-report.txt", report)
        upload_text_file(drive, run_folder["id"], "vacancies.json", vacancies_text, mime_type="application/json")
        upload_text_file(drive, state_folder["id"], "seen-vacancies.json", state_text, mime_type="application/json")
        print(f"Drive project folder: {project_folder.get('webViewLink')}")
        print(f"Drive runs folder: {runs_folder.get('webViewLink')}")
        print(f"Drive run folder: {run_folder.get('webViewLink')}")
    else:
        (run_dir / "email-report.txt").write_text(report, encoding="utf-8")
        (run_dir / "vacancies.json").write_text(vacancies_text, encoding="utf-8")
        args.state_file.write_text(state_text, encoding="utf-8")
        print(run_dir / "email-report.txt")
    print(f"Wrote {len(vacancies)} vacancies ({len(new_items)} new)")


if __name__ == "__main__":
    main()
