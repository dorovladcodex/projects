#!/usr/bin/env python3
"""Google Health Connect ZIP delta loader.

Downloads a Health Connect export ZIP from Google Drive, extracts supported data
files, filters records from a start date, and writes a deduplicated CSV back to
the same Drive folder.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload


SCOPES = ["https://www.googleapis.com/auth/drive"]

DEFAULT_SOURCE_FILE_ID = "1W8Q9a2eTqoipQafoetieW_ka5CB7Qof5"
DEFAULT_START_DATE = "2026-01-01"
DEFAULT_OUTPUT_CSV_NAME = "health_data.csv"
DEFAULT_OUTPUT_FOLDER_PATH = "projects/health monitor"
STATE_JSON_NAME = "health_delta_state.json"

CSV_FIELDS = [
    "record_hash",
    "source_zip_file_id",
    "source_zip_modified_time",
    "zip_member",
    "record_type",
    "record_time",
    "record_date",
    "value",
    "unit",
    "raw_json",
]

TIME_KEYS = [
    "startTime",
    "start_time",
    "time",
    "timestamp",
    "dateTime",
    "date_time",
    "endTime",
    "end_time",
    "date",
    "day",
    "epoch_millis",
    "access_time",
    "read_time",
    "write_time",
    "stage_start_time",
    "lap_start_time",
    "segment_start_time",
    "timestamp_millis",
]

NESTED_TIME_KEYS = [
    "lastModifiedTime",
    "last_modified_time",
    "modifiedTime",
    "createdTime",
    "updateTime",
]


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    modified_time: str
    parents: list[str]


@dataclass
class RunStats:
    scanned_records: int = 0
    added_rows: int = 0
    skipped_before_start: int = 0
    skipped_no_time: int = 0
    skipped_duplicate: int = 0


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def get_drive_service(credentials_path: Path, token_path: Path) -> Any:
    creds: Credentials | None = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing Google OAuth token...")
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"OAuth credentials file not found: {credentials_path}. "
                    "Create Google OAuth Desktop credentials and save them as credentials.json next to the script."
                )
            print("Opening browser for Google OAuth authorization...")
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)

        token_path.write_text(creds.to_json(), encoding="utf-8")
        print(f"Saved OAuth token to {token_path}")

    return build("drive", "v3", credentials=creds)


def get_drive_file(service: Any, file_id: str) -> DriveFile:
    metadata = (
        service.files()
        .get(
            fileId=file_id,
            fields="id,name,modifiedTime,parents",
            supportsAllDrives=True,
        )
        .execute()
    )
    return DriveFile(
        id=metadata["id"],
        name=metadata["name"],
        modified_time=metadata.get("modifiedTime", ""),
        parents=metadata.get("parents", []),
    )


def escape_drive_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def find_drive_file_in_folder(service: Any, folder_id: str, name: str) -> dict[str, Any] | None:
    query = (
        f"'{escape_drive_query_value(folder_id)}' in parents and "
        f"name = '{escape_drive_query_value(name)}' and trashed = false"
    )
    response = (
        service.files()
        .list(
            q=query,
            fields="files(id,name,mimeType,modifiedTime,size,webViewLink)",
            pageSize=10,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = response.get("files", [])
    return files[0] if files else None


def find_child_folder(service: Any, parent_id: str, folder_name: str) -> dict[str, Any] | None:
    query = (
        f"'{escape_drive_query_value(parent_id)}' in parents and "
        f"name = '{escape_drive_query_value(folder_name)}' and "
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    response = (
        service.files()
        .list(
            q=query,
            fields="files(id,name,webViewLink)",
            pageSize=10,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = response.get("files", [])
    return files[0] if files else None


def create_child_folder(service: Any, parent_id: str, folder_name: str) -> dict[str, Any]:
    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    return (
        service.files()
        .create(
            body=metadata,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )


def get_or_create_folder_path(service: Any, folder_path: str) -> dict[str, Any]:
    current_id = "root"
    current_folder = {"id": current_id, "name": "My Drive"}

    parts = [part.strip() for part in folder_path.replace("\\", "/").split("/") if part.strip()]
    if not parts:
        return current_folder

    for part in parts:
        existing = find_child_folder(service, current_id, part)
        if existing:
            current_folder = existing
        else:
            print(f"Creating Drive folder: {part}")
            current_folder = create_child_folder(service, current_id, part)
        current_id = current_folder["id"]

    return current_folder


def download_drive_file(service: Any, file_id: str, destination: Path) -> None:
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with destination.open("wb") as handle:
        downloader = MediaIoBaseDownload(handle, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"Download progress: {int(status.progress() * 100)}%")


def download_drive_text_file(service: Any, file_id: str) -> str:
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue().decode("utf-8")


def upload_or_update_drive_file(
    service: Any,
    folder_id: str,
    local_path: Path,
    drive_name: str,
    mime_type: str,
) -> dict[str, Any]:
    existing = find_drive_file_in_folder(service, folder_id, drive_name)
    media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)

    if existing:
        print(f"Updating {drive_name} on Google Drive...")
        return (
            service.files()
            .update(
                fileId=existing["id"],
                media_body=media,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )

    print(f"Creating {drive_name} on Google Drive...")
    metadata = {"name": drive_name, "parents": [folder_id]}
    return (
        service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )


def load_state(service: Any, folder_id: str) -> dict[str, Any]:
    existing = find_drive_file_in_folder(service, folder_id, STATE_JSON_NAME)
    if not existing:
        return {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_source_modified_time": None,
            "processed_hashes_count": 0,
            "runs": [],
        }

    try:
        return json.loads(download_drive_text_file(service, existing["id"]))
    except (json.JSONDecodeError, HttpError, OSError) as exc:
        print(f"Warning: could not read existing state file; recreating it. Reason: {exc}")
        return {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_source_modified_time": None,
            "processed_hashes_count": 0,
            "runs": [],
            "warning": "Previous state could not be read and was recreated.",
        }


def extract_zip(zip_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = safe_member_path(destination, member.filename)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def safe_member_path(extract_root: Path, member_name: str) -> Path:
    path = (extract_root / member_name).resolve()
    root = extract_root.resolve()
    if path == root or root in path.parents:
        return path
    raise ValueError(f"Unsafe ZIP member path skipped: {member_name}")


def parse_datetime_maybe(value: Any) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        return value

    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)

    if isinstance(value, (int, float)):
        try:
            if value > 10_000_000_000:
                return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def find_time_in_mapping(mapping: dict[str, Any], keys: list[str]) -> datetime | None:
    for key in keys:
        if key in mapping:
            parsed = parse_datetime_maybe(mapping[key])
            if parsed:
                return parsed
    return None


def find_record_time(record: dict[str, Any]) -> datetime | None:
    parsed = find_time_in_mapping(record, TIME_KEYS)
    if parsed:
        return parsed

    for nested_name in ("metadata", "dataOrigin", "device", "recordingMethod"):
        nested = record.get(nested_name)
        if isinstance(nested, dict):
            parsed = find_time_in_mapping(nested, TIME_KEYS + NESTED_TIME_KEYS)
            if parsed:
                return parsed

    return None


def guess_record_type(record: dict[str, Any], zip_member: str) -> str:
    for key in ("__record_type", "recordType", "record_type", "type", "dataType", "data_type", "name"):
        value = record.get(key)
        if value:
            return str(value)
    return Path(zip_member).stem


def guess_value_and_unit(record: dict[str, Any]) -> tuple[str, str]:
    value_keys = (
        "value",
        "count",
        "distance",
        "energy",
        "mass",
        "weight",
        "beatsPerMinute",
        "percentage",
        "duration",
        "steps",
        "energy",
        "basal_metabolic_rate",
        "systolic",
        "diastolic",
        "body_water_mass",
        "mass",
        "height",
        "level",
        "temperature",
        "rate",
        "beats_per_minute",
        "weight",
        "vo2_milliliters_per_minute_kilogram",
        "speed",
        "floors",
        "volume",
        "elevation",
    )
    unit_keys = ("unit", "valueUnit", "distanceUnit", "energyUnit", "massUnit")

    value: Any = ""
    unit: Any = ""

    for key in value_keys:
        if record.get(key) is not None:
            value = record[key]
            break

    for key in unit_keys:
        if record.get(key) is not None:
            unit = record[key]
            break

    if isinstance(value, dict):
        if len(value) == 1:
            unit, value = next(iter(value.items()))
        else:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)

    return str(value), str(unit)


def json_safe_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def stable_hash(value: Any) -> str:
    normalized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def iter_json_container_records(obj: Any) -> Iterator[dict[str, Any]]:
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                yield item
        return

    if isinstance(obj, dict):
        for key in ("records", "data", "entries", "items"):
            items = obj.get(key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        yield item
                return
        yield obj


def iter_json_records(path: Path) -> Iterator[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return

    try:
        yield from iter_json_container_records(json.loads(text))
    except json.JSONDecodeError as exc:
        print(f"Warning: skipped invalid JSON file {path.name}: {exc}")


def iter_json_lines_records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                print(f"Warning: skipped invalid JSON line {path.name}:{line_number}")
                continue
            if isinstance(obj, dict):
                yield obj


def iter_csv_records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            yield dict(row)


def quote_sql_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def should_read_sqlite_table(table_name: str) -> bool:
    lower_name = table_name.lower()
    return (
        lower_name.endswith("_record_table")
        or lower_name.endswith("_record_series_table")
        or lower_name.endswith("_stages_table")
        or lower_name.endswith("recordtable")
    )


def iter_sqlite_records(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row

    try:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()

        for table_row in tables:
            table_name = table_row["name"]
            if not should_read_sqlite_table(table_name):
                continue

            quoted_table = quote_sql_identifier(table_name)

            try:
                columns = connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()
                column_names = [column["name"] for column in columns]
                if not any(column in set(TIME_KEYS + NESTED_TIME_KEYS) for column in column_names):
                    continue

                cursor = connection.execute(f"SELECT * FROM {quoted_table}")
                for row in cursor:
                    record = {key: json_safe_value(row[key]) for key in row.keys()}
                    record["__record_type"] = table_name
                    yield table_name, record
            except sqlite3.DatabaseError as exc:
                print(f"Warning: skipped SQLite table {table_name}: {exc}")
    finally:
        connection.close()


def iter_xml_records(_path: Path) -> Iterator[dict[str, Any]]:
    # Placeholder for a future XML parser if Health Connect exports XML records.
    return
    yield


def iter_health_records(extract_root: Path, member_names: Iterable[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    for member_name in member_names:
        lower_name = member_name.lower()
        if lower_name.endswith("/"):
            continue

        try:
            path = safe_member_path(extract_root, member_name)
        except ValueError as exc:
            print(f"Warning: {exc}")
            continue

        if not path.is_file():
            continue

        if lower_name.endswith(".jsonl") or lower_name.endswith(".ndjson"):
            for record in iter_json_lines_records(path):
                yield member_name, record
        elif lower_name.endswith(".json"):
            for record in iter_json_records(path):
                yield member_name, record
        elif lower_name.endswith(".csv"):
            for record in iter_csv_records(path):
                yield member_name, record
        elif lower_name.endswith(".db") or lower_name.endswith(".sqlite") or lower_name.endswith(".sqlite3"):
            for table_name, record in iter_sqlite_records(path):
                yield f"{member_name}::{table_name}", record
        elif lower_name.endswith(".xml"):
            print(f"Skipping XML file for now: {member_name}")
            for record in iter_xml_records(path):
                yield member_name, record


def build_csv_row(
    record: dict[str, Any],
    zip_member: str,
    source_file_id: str,
    source_modified_time: str,
) -> dict[str, str] | None:
    record_time = find_record_time(record)
    if not record_time:
        return None

    record_type = guess_record_type(record, zip_member)
    value, unit = guess_value_and_unit(record)
    raw_json = json.dumps(record, ensure_ascii=False, sort_keys=True)
    record_hash = stable_hash(
        {
            "zip_member": zip_member,
            "record_type": record_type,
            "record_time": record_time.isoformat(),
            "record": record,
        }
    )

    return {
        "record_hash": record_hash,
        "source_zip_file_id": source_file_id,
        "source_zip_modified_time": source_modified_time,
        "zip_member": zip_member,
        "record_type": record_type,
        "record_time": record_time.isoformat(),
        "record_date": record_time.date().isoformat(),
        "value": value,
        "unit": unit,
        "raw_json": raw_json,
    }


def download_existing_csv_if_present(
    service: Any,
    folder_id: str,
    csv_name: str,
    destination: Path,
) -> dict[str, Any] | None:
    existing = find_drive_file_in_folder(service, folder_id, csv_name)
    if not existing:
        return None

    print(f"Downloading existing {csv_name} for deduplication...")
    download_drive_file(service, existing["id"], destination)
    return existing


def load_existing_hashes(csv_path: Path) -> set[str]:
    hashes: set[str] = set()
    if not csv_path.exists():
        return hashes

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            record_hash = row.get("record_hash")
            if record_hash:
                hashes.add(record_hash)
    return hashes


def append_rows_to_csv(csv_path: Path, rows: list[dict[str, str]]) -> None:
    file_exists = csv_path.exists()
    with csv_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def process_extracted_records(
    extract_root: Path,
    member_names: Iterable[str],
    existing_hashes: set[str],
    start_date: date,
    source_file_id: str,
    source_modified_time: str,
) -> tuple[list[dict[str, str]], RunStats]:
    rows: list[dict[str, str]] = []
    stats = RunStats()

    for zip_member, record in iter_health_records(extract_root, member_names):
        stats.scanned_records += 1
        row = build_csv_row(record, zip_member, source_file_id, source_modified_time)

        if row is None:
            stats.skipped_no_time += 1
            continue

        if date.fromisoformat(row["record_date"]) < start_date:
            stats.skipped_before_start += 1
            continue

        if row["record_hash"] in existing_hashes:
            stats.skipped_duplicate += 1
            continue

        existing_hashes.add(row["record_hash"])
        rows.append(row)

    stats.added_rows = len(rows)
    return rows, stats


def make_run_info(
    source: DriveFile,
    start_date: date,
    force: bool,
    stats: RunStats,
) -> dict[str, Any]:
    return {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "source_file_id": source.id,
        "source_name": source.name,
        "source_modified_time": source.modified_time,
        "start_date_filter_inclusive": start_date.isoformat(),
        "force": force,
        "scanned_records": stats.scanned_records,
        "added_rows": stats.added_rows,
        "skipped_before_start": stats.skipped_before_start,
        "skipped_no_time": stats.skipped_no_time,
        "skipped_duplicate": stats.skipped_duplicate,
    }


def parse_args() -> argparse.Namespace:
    default_credentials = script_dir() / "credentials.json"
    default_token = script_dir() / "token.json"

    parser = argparse.ArgumentParser(description="Load Google Health Connect ZIP deltas into a Drive CSV.")
    parser.add_argument("--source-file-id", default=DEFAULT_SOURCE_FILE_ID)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--output-csv-name", default=DEFAULT_OUTPUT_CSV_NAME)
    parser.add_argument("--output-folder-path", default=DEFAULT_OUTPUT_FOLDER_PATH)
    parser.add_argument("--credentials", type=Path, default=default_credentials)
    parser.add_argument("--token", type=Path, default=default_token)
    parser.add_argument("--force", action="store_true", help="Reprocess even if the ZIP modifiedTime did not change.")
    return parser.parse_args()


def run() -> None:
    args = parse_args()
    start_date = date.fromisoformat(args.start_date)
    csv_name = args.output_csv_name

    service = get_drive_service(args.credentials, args.token)
    source = get_drive_file(service, args.source_file_id)

    output_folder = get_or_create_folder_path(service, args.output_folder_path)
    folder_id = output_folder["id"]
    print(f"Output Drive folder: {args.output_folder_path}")

    state = load_state(service, folder_id)

    if not args.force and state.get("last_source_modified_time") == source.modified_time:
        print("Source ZIP modifiedTime has not changed since the previous run. No new delta to process.")
        return

    with tempfile.TemporaryDirectory(prefix="health-connect-delta-") as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        zip_path = tmp_dir / source.name
        extract_root = tmp_dir / "extracted"
        local_csv = tmp_dir / csv_name
        local_state = tmp_dir / STATE_JSON_NAME

        print(f"Downloading source ZIP: {source.name}")
        download_drive_file(service, source.id, zip_path)

        print("Extracting ZIP to a temporary folder...")
        extract_root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as archive:
            member_names = archive.namelist()
        extract_zip(zip_path, extract_root)

        existing_csv = download_existing_csv_if_present(service, folder_id, csv_name, local_csv)
        existing_hashes = load_existing_hashes(local_csv)
        print(f"Loaded {len(existing_hashes)} existing record hashes.")

        rows, stats = process_extracted_records(
            extract_root=extract_root,
            member_names=member_names,
            existing_hashes=existing_hashes,
            start_date=start_date,
            source_file_id=source.id,
            source_modified_time=source.modified_time,
        )

        if rows:
            append_rows_to_csv(local_csv, rows)
            csv_upload = upload_or_update_drive_file(service, folder_id, local_csv, csv_name, "text/csv")
            csv_location = csv_upload.get("webViewLink") or csv_upload.get("id")
        elif existing_csv:
            csv_location = existing_csv.get("webViewLink") or existing_csv.get("id")
            print("No new rows to add; existing CSV was left unchanged.")
        else:
            append_rows_to_csv(local_csv, [])
            csv_upload = upload_or_update_drive_file(service, folder_id, local_csv, csv_name, "text/csv")
            csv_location = csv_upload.get("webViewLink") or csv_upload.get("id")

        run_info = make_run_info(source, start_date, args.force, stats)
        state["last_source_modified_time"] = source.modified_time
        state["processed_hashes_count"] = len(existing_hashes)
        state.setdefault("runs", []).append(run_info)

        local_state.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        upload_or_update_drive_file(service, folder_id, local_state, STATE_JSON_NAME, "application/json")

        shutil.rmtree(extract_root, ignore_errors=True)

    print("Run summary:")
    print(json.dumps(run_info, ensure_ascii=False, indent=2))
    print(f"CSV: {csv_location}")


def main() -> None:
    try:
        run()
    except HttpError as exc:
        raise SystemExit(f"Google Drive API error: {exc}") from exc
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    except ValueError as exc:
        raise SystemExit(f"Invalid argument or data format: {exc}") from exc
    except zipfile.BadZipFile as exc:
        raise SystemExit(f"Source file is not a valid ZIP archive: {exc}") from exc


if __name__ == "__main__":
    main()
