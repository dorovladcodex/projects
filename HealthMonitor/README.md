# Health Connect Delta Loader

This script reads a shared Google Health Connect export ZIP from Google Drive, extracts supported health records, filters records from `2026-01-01`, and writes daily deltas into a CSV file on Google Drive.

Default setup:

- Source ZIP file ID: `1W8Q9a2eTqoipQafoetieW_ka5CB7Qof5`
- Source ZIP access: shared with the OAuth Google account, for example `dorovlad.codex@gmail.com`
- Start date: `2026-01-01`
- Output Drive folder: `projects/health monitor`
- Output CSV: `health_data.csv`
- State file: `health_delta_state.json`

The output folder is created automatically if it does not exist.

## Supported ZIP Contents

The loader currently reads:

- `.json`
- `.jsonl`
- `.ndjson`
- `.csv`
- `.db`, `.sqlite`, `.sqlite3` SQLite exports

XML files are detected and skipped for now. The code has a placeholder for a future XML parser.

## Google OAuth Credentials

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create or select a project.
3. Enable the Google Drive API.
4. Go to **APIs & Services** -> **OAuth consent screen**.
5. Add the Google account that will run the script as a test user.
6. Go to **APIs & Services** -> **Credentials**.
7. Create an **OAuth client ID**.
8. Choose **Desktop app**.
9. Download the JSON file.
10. Rename it to `credentials.json`.
11. Put `credentials.json` next to `health_connect_delta_loader.py`.

On the first run, the script opens a browser for Google OAuth authorization and creates `token.json` next to the script. Do not commit `credentials.json` or `token.json`.

## Install

macOS/Linux:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## First Run

```bash
python health_connect_delta_loader.py
```

Windows PowerShell with the local venv:

```powershell
.\.venv\Scripts\python.exe health_connect_delta_loader.py
```

The script will:

1. Connect to Google Drive with OAuth.
2. Download `Health Connect.zip` by file ID.
3. Create or find the Drive folder `projects/health monitor`.
4. Extract the ZIP into a temporary local folder.
5. Read supported files inside the ZIP.
6. Keep only records with date/time greater than or equal to `2026-01-01`.
7. Create or update `health_data.csv` in `projects/health monitor`.
8. Create or update `health_delta_state.json` in `projects/health monitor`.

## Delta Mode

Each processed record gets a stable SHA-256 hash based on normalized record content. On later runs, the script downloads the existing CSV, loads existing hashes, and appends only records that are not already present.

The state file stores:

- `last_source_modified_time`
- `processed_hashes_count`
- run history
- `scanned_records`
- `added_rows`
- `skipped_before_start`
- `skipped_no_time`
- `skipped_duplicate`

If the source ZIP `modifiedTime` has not changed since the last run, the script exits early with a message saying there is no new delta.

## Change Start Date

```bash
python health_connect_delta_loader.py --start-date 2026-06-15
```

## Force Reprocessing

Use `--force` to process the ZIP even when Google Drive `modifiedTime` has not changed:

```bash
python health_connect_delta_loader.py --force
```

## Change Source ZIP

```bash
python health_connect_delta_loader.py --source-file-id YOUR_GOOGLE_DRIVE_FILE_ID
```

## Change Output Location

```bash
python health_connect_delta_loader.py --output-folder-path "projects/health monitor" --output-csv-name health_data.csv
```

## CSV Columns

The output CSV columns are:

- `record_hash`
- `source_zip_file_id`
- `source_zip_modified_time`
- `zip_member`
- `record_type`
- `record_time`
- `record_date`
- `value`
- `unit`
- `raw_json`
