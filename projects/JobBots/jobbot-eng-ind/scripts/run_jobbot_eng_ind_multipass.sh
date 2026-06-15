#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DATE_UTC="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
RUN_DIR="${JOBBOT_RUN_DIR:-$ROOT_DIR/.manual-runs/$DATE_UTC JobBot Eng Ind Multipass}"
RAW_DIR="$RUN_DIR/raw-source-passes"
PUBLIC_RAW_JSON="$RAW_DIR/public-apis.json"
PUBLIC_COVERAGE_JSON="$RUN_DIR/public-source-coverage.json"
RAW_JSON="$RUN_DIR/raw-vacancies.json"
VALID_JSON="$RUN_DIR/vacancies.json"
COVERAGE_JSON="$RUN_DIR/source-coverage.json"
REJECTS_JSON="$RUN_DIR/rejected-vacancies.json"
STATE_FILE="${JOBBOT_STATE_FILE:-$ROOT_DIR/.manual-runs/state/seen-vacancies.json}"
REPORT_ROOT="$RUN_DIR/output"
SCHEMA_FILE="$ROOT_DIR/schemas/vacancies.schema.json"

mkdir -p "$RAW_DIR" "$REPORT_ROOT" "$(dirname "$STATE_FILE")"
if [[ ! -f "$STATE_FILE" ]]; then
  printf '{}\n' > "$STATE_FILE"
fi

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ -n "${CODEX_BIN:-}" ]]; then
  :
elif command -v codex >/dev/null 2>&1; then
  CODEX_BIN="$(command -v codex)"
elif [[ -x "$HOME/.local/bin/codex" ]]; then
  CODEX_BIN="$HOME/.local/bin/codex"
elif [[ -x "$HOME/.codex/packages/standalone/releases/0.139.0-x86_64-unknown-linux-musl/bin/codex" ]]; then
  CODEX_BIN="$HOME/.codex/packages/standalone/releases/0.139.0-x86_64-unknown-linux-musl/bin/codex"
else
  echo "codex binary not found. Set CODEX_BIN explicitly." >&2
  exit 127
fi

"$PYTHON_BIN" "$ROOT_DIR/src/fetch_public_sources.py" \
  --output "$PUBLIC_RAW_JSON" \
  --coverage-output "$PUBLIC_COVERAGE_JSON"

run_source_pass() {
  local slug="$1"
  local source_group="$2"
  local query_block="$3"
  local output_file="$RAW_DIR/$slug.json"

  printf 'Running source pass: %s\n' "$source_group"
  "$CODEX_BIN" \
    --search \
    --sandbox workspace-write \
    --ask-for-approval never \
    --cd "$ROOT_DIR" \
    exec \
    --output-schema "$SCHEMA_FILE" \
    -o "$output_file" \
    "$(cat <<PROMPT
You are running a source-specific pass for JobBot Eng Ind.

Return VALID JSON ONLY: one object with top-level field "vacancies".
Return up to 15 high-quality current vacancies from source group: $source_group.
If you find no concrete high-quality vacancies for this source group, return {"vacancies":[]}.

Candidate:
- Volodymyr, near Dortmund, NRW, Germany.
- German B1 improving toward B2; English-friendly strongly preferred.
- Target compensation EUR 85,000+ gross/year where realistic.
- Senior Oracle PL/SQL, SQL, ERP/WMS/logistics/retail/enterprise systems.
- Moving toward Azure Databricks, Data Platform, Lakehouse, Spark/PySpark, Python, Azure Data Factory, Microsoft Fabric.

Include:
- Senior Data Engineer, Azure Data Engineer, Databricks Data Engineer, Cloud Data Engineer, Data Platform Engineer, Data Lakehouse Engineer.
- Analytics Engineer only if clearly engineering-heavy.
- Germany/EU remote roles compatible with Germany-based candidates.
- NRW hybrid/local roles near Dortmund, Essen, Duesseldorf, Bochum, Duisburg, Cologne/Koeln, Wuppertal, Ratingen, Neuss.
- Strong freelance/contract roles if technically strong and remote-friendly.

Exclude:
- Full onsite, junior, internships, student roles.
- Pure BI/reporting unless clearly engineering-heavy.
- German C1/C2 roles unless exceptional.
- Remote roles restricted to US/UK/Spain/Poland or incompatible with Germany.
- Generic search pages, career homepages, category pages, or inferred vacancies.

URL rules:
- Include only concrete vacancy detail pages.
- Indexed detail pages are allowed if they clearly correspond to one current vacancy.
- Do not invent vacancies from company pages.

Ranking:
- Rank by technical match, English friendliness, compensation likelihood, URL quality, Germany compatibility, NRW/remote practicality, seniority, and domain relevance.

Output schema:
Each vacancy must contain exactly:
- priority: one of A, A-, B+, B, B-, C
- company
- title
- location
- work_mode
- salary
- source
- url
- language
- contract_type
- core_tech_match
- fit_score: number 1-10
- status: NEW
- analysis
- recommendation
- found_at: ISO timestamp
- source_group: exactly "$source_group"
- language_risk: low, medium, or high
- salary_likelihood: low, medium, high, or unknown

Search only this source group. Do not fill with another source group.

Required searches:
$query_block
PROMPT
)"
}

run_source_pass "linkedin" "LinkedIn-indexed" "
- site:linkedin.com/jobs/view Senior Data Engineer Germany Azure
- site:linkedin.com/jobs/view Databricks Data Engineer Germany
- site:linkedin.com/jobs/view Data Platform Engineer Germany remote
- linkedin.com/jobs/view \"Senior Data Engineer\" \"Germany\" \"Azure\"
"

run_source_pass "stepstone" "StepStone-indexed" "
- site:stepstone.de/stellenangebote-- Senior Data Engineer Azure Germany
- site:stepstone.de/stellenangebote-- Databricks Data Engineer
- site:stepstone.de/stellenangebote-- Data Platform Engineer NRW
- stepstone senior data engineer azure germany
"

run_source_pass "indeed" "Indeed-indexed" "
- site:indeed.com/viewjob Senior Data Engineer Germany Azure
- site:de.indeed.com/viewjob Databricks Data Engineer
- site:indeed.com/viewjob Data Platform Engineer Germany remote
- indeed senior data engineer azure germany
"

run_source_pass "glassdoor" "Glassdoor-indexed" "
- site:glassdoor.de/Job Senior Data Engineer Germany Azure
- site:glassdoor.de/Job Databricks Data Engineer Germany
- glassdoor senior data engineer azure germany
"

run_source_pass "germantechjobs" "GermanTechJobs/get-in-IT/WeAreDevelopers" "
- site:germantechjobs.de/jobs Senior Data Engineer Azure Germany
- site:germantechjobs.de/jobs Databricks Data Engineer Germany
- site:get-in-it.de/jobsuche Senior Data Engineer Azure
- site:wearedevelopers.com/en/companies Data Engineer Germany Azure
"

run_source_pass "arbeitnow" "Arbeitnow/EnglishJobs" "
- site:arbeitnow.com/jobs/companies Senior Data Engineer Germany
- site:arbeitnow.com/jobs/companies Databricks Data Engineer Germany
- site:englishjobs.de/jobs Senior Data Engineer Germany
- Arbeitnow Senior Data Engineer Germany Azure
"

run_source_pass "remote-boards" "Remote boards" "
- site:remoteok.com/remote-jobs Data Engineer Europe Germany
- site:remotive.com/remote-jobs Data Engineer Europe Germany
- site:euremotejobs.com/job Senior Data Engineer
- site:dailyremote.com/remote-job Senior Data Engineer Europe
- site:remoterocketship.com Senior Data Engineer Germany
"

run_source_pass "freelance" "Freelance/project boards" "
- site:freelancermap.de/projekt Senior Data Engineer Azure remote
- site:freelancermap.de/projekt Databricks Data Engineer remote
- site:freelance.de/Projekte Senior Data Engineer Azure
- freelance project Azure Data Engineer Germany remote
"

run_source_pass "company-careers" "Company career pages" "
- site:jobs.lever.co Senior Data Engineer Germany Databricks
- site:boards.greenhouse.io Senior Data Engineer Germany Databricks
- site:jobs.smartrecruiters.com Senior Data Engineer Germany Azure
- site:workdayjobs.com Senior Data Engineer Germany Databricks
- site:careers.* Senior Data Engineer Germany Databricks
"

run_source_pass "recruiters" "Recruiters" "
- senior data engineer azure databricks germany recruiter
- senior data engineer databricks germany recruitment
- site:workwise.io Senior Data Engineer Germany Azure
- site:join.com/companies Senior Data Engineer Germany Azure
"

"$PYTHON_BIN" "$ROOT_DIR/src/merge_vacancies.py" \
  --output "$RAW_JSON" \
  "$RAW_DIR"/*.json

"$PYTHON_BIN" "$ROOT_DIR/src/validate_and_rank.py" \
  --input "$RAW_JSON" \
  --output "$VALID_JSON" \
  --coverage-output "$COVERAGE_JSON" \
  --rejects-output "$REJECTS_JSON"

"$PYTHON_BIN" "$ROOT_DIR/src/jobbot_eng_ind.py" \
  --vacancies-json "$VALID_JSON" \
  --output-root "$REPORT_ROOT" \
  --state-file "$STATE_FILE" \
  ${JOBBOT_NEW_ONLY:+--new-only}

REPORT_FILE="$(find "$REPORT_ROOT" -name email-report.txt -type f | sort | tail -n 1)"

if [[ "${JOBBOT_ENABLE_GMAIL:-1}" == "1" ]]; then
  DELIVERY_MODE="${JOBBOT_GMAIL_DELIVERY_MODE:-codex-plugin}"
  if [[ "$DELIVERY_MODE" == "codex-plugin" ]]; then
    bash "$ROOT_DIR/scripts/send_report_via_codex_gmail.sh" \
      "${JOBBOT_EMAIL_TO:-dorovlad@gmail.com}" \
      "JobBot Eng Ind - English-friendly job results - $(date +%Y-%m-%d)" \
      "$REPORT_FILE" \
      "$VALID_JSON"
  else
    "$PYTHON_BIN" "$ROOT_DIR/src/send_gmail.py" \
      --to "${JOBBOT_EMAIL_TO:-dorovlad@gmail.com}" \
      --subject "JobBot Eng Ind - English-friendly job results - $(date +%Y-%m-%d)" \
      --body-file "$REPORT_FILE" \
      --attachment "$VALID_JSON" \
      --google-client-file "${GOOGLE_CLIENT_FILE:-$ROOT_DIR/credentials/google-oauth-client.json}" \
      --gmail-token-file "${GMAIL_TOKEN_FILE:-$ROOT_DIR/credentials/gmail-token.json}"
  fi
fi

echo "Run directory: $RUN_DIR"
echo "Validated vacancies: $VALID_JSON"
echo "State file: $STATE_FILE"
echo "Report file: $REPORT_FILE"
