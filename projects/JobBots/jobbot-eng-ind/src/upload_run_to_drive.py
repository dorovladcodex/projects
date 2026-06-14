from __future__ import annotations

import argparse
from pathlib import Path

from drive_store import build_drive_service, ensure_path, upload_file


DEFAULT_DRIVE_PATH = ["Projects", "JobBots", "jobbot-eng-ind"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload a JobBot Eng Ind run folder to Google Drive")
    parser.add_argument("--run-dir", required=True, type=Path, help="Folder containing generated report files")
    parser.add_argument("--state-file", required=True, type=Path, help="seen-vacancies.json to upload to Drive state")
    parser.add_argument("--run-name", help="Drive run folder name. Defaults to local run folder name.")
    parser.add_argument("--google-client-file", type=Path, default=Path("credentials/google-oauth-client.json"))
    parser.add_argument("--google-token-file", type=Path, default=Path("credentials/google-token.json"))
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    state_file = args.state_file.resolve()
    report_file = run_dir / "email-report.txt"
    vacancies_file = run_dir / "vacancies.json"
    if not report_file.exists():
        raise FileNotFoundError(report_file)
    if not vacancies_file.exists():
        raise FileNotFoundError(vacancies_file)
    if not state_file.exists():
        raise FileNotFoundError(state_file)

    drive = build_drive_service(args.google_client_file, args.google_token_file)
    project_folder = ensure_path(drive, DEFAULT_DRIVE_PATH)
    state_folder = ensure_path(drive, [*DEFAULT_DRIVE_PATH, "state"])
    ensure_path(drive, [*DEFAULT_DRIVE_PATH, "resumes"])
    run_folder = ensure_path(drive, [*DEFAULT_DRIVE_PATH, "runs", args.run_name or run_dir.name])

    report_link = upload_file(drive, run_folder["id"], report_file, mime_type="text/plain")
    vacancies_link = upload_file(drive, run_folder["id"], vacancies_file, mime_type="application/json")
    state_link = upload_file(drive, state_folder["id"], state_file, mime_type="application/json")

    print(f"Project folder: {project_folder.get('webViewLink')}")
    print(f"Run folder: {run_folder.get('webViewLink')}")
    print(f"Report: {report_link}")
    print(f"Vacancies: {vacancies_link}")
    print(f"State: {state_link}")


if __name__ == "__main__":
    main()
