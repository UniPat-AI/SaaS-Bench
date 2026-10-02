"""
Verifier for Business-302-I4: End-to-End Recruitment Pipeline for Business Analyst

Checks: 14 weighted checks across hrms, bigcapital, twenty.
Strategy: docker exec MariaDB (hrms), REST API + docker exec MariaDB (bigcapital),
docker exec Postgres (twenty)

Required env vars:
  SERVER_HOSTNAME, HRMS_PORT, HRMS_CONTAINER, HRMS_DB_CONTAINER,
  BIGCAPITAL_PORT, BIGCAPITAL_CONTAINER, BIGCAPITAL_DB_CONTAINER,
  TWENTY_PORT, TWENTY_CONTAINER, TWENTY_DB_CONTAINER
"""

import html
import os
import re
import sys
import subprocess
from datetime import datetime, timedelta

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

HRMS_PORT = os.environ.get("HRMS_PORT")
HRMS_CONTAINER = os.environ.get("HRMS_CONTAINER")
HRMS_DB_CONTAINER = os.environ.get("HRMS_DB_CONTAINER")

BIGCAPITAL_PORT = os.environ.get("BIGCAPITAL_PORT")
BIGCAPITAL_CONTAINER = os.environ.get("BIGCAPITAL_CONTAINER")
BIGCAPITAL_DB_CONTAINER = os.environ.get("BIGCAPITAL_DB_CONTAINER")

TWENTY_PORT = os.environ.get("TWENTY_PORT")
TWENTY_CONTAINER = os.environ.get("TWENTY_CONTAINER")
TWENTY_DB_CONTAINER = os.environ.get("TWENTY_DB_CONTAINER")

_required = {
    "HRMS_PORT": HRMS_PORT,
    "HRMS_CONTAINER": HRMS_CONTAINER,
    "HRMS_DB_CONTAINER": HRMS_DB_CONTAINER,
    "BIGCAPITAL_PORT": BIGCAPITAL_PORT,
    "BIGCAPITAL_CONTAINER": BIGCAPITAL_CONTAINER,
    "BIGCAPITAL_DB_CONTAINER": BIGCAPITAL_DB_CONTAINER,
    "TWENTY_PORT": TWENTY_PORT,
    "TWENTY_CONTAINER": TWENTY_CONTAINER,
    "TWENTY_DB_CONTAINER": TWENTY_DB_CONTAINER,
}
for _var, _val in _required.items():
    if not _val:
        print(f"FATAL: {_var} not set", file=sys.stderr)
        sys.exit(1)


# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    tail = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{tail}", file=sys.stderr)


# ── Helpers ───────────────────────────────────────────────────────────────────
def docker_exec(container: str, *args: str, timeout: int = 15) -> tuple[int, str, str]:
    r = subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


def _norm(text: str | None) -> str:
    """Normalize for substring comparison: HTML-unescape, dash-normalize,
    fold all whitespace (incl. nbsp) to single spaces."""
    if not text:
        return ""
    s = html.unescape(text)
    s = s.replace("—", "-").replace("–", "-").replace("--", "-")
    s = s.replace(" ", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _contains(haystack: str | None, needle: str) -> bool:
    """Case-insensitive normalized substring test (for rich-text/HTML fields)."""
    return _norm(needle).lower() in _norm(haystack).lower()


def date_matches_tz(stored: str | None, expected: str) -> bool:
    """A stored UTC timestamp (or bare date) matches local calendar date
    `expected` if its UTC date or its local-offset-shifted date equals it.
    Local offset is taken from the verifier host, not hardcoded."""
    s = (stored or "").strip()
    if not s or s.upper() == "NULL":
        return False
    if len(s) == 10:
        return s == expected
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", s)
    if not m:
        return False
    dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    return (dt.date().isoformat() == expected
            or (dt + offset).date().isoformat() == expected)


_hrms_db_name: str | None = None


def _detect_hrms_db() -> str:
    """Find the Frappe site DB (has tabJob Applicant table)."""
    global _hrms_db_name
    if _hrms_db_name:
        return _hrms_db_name
    rc, out, err = docker_exec(
        HRMS_DB_CONTAINER,
        "mysql", "-uroot", f"-p{os.environ.get('HRMS_DB_ROOT_PASSWORD', 'hrms123456')}",
        "--default-character-set=utf8mb4", "-N", "-e",
        "SELECT TABLE_SCHEMA FROM information_schema.TABLES "
        "WHERE TABLE_NAME='tabJob Applicant' LIMIT 1",
    )
    if rc != 0:
        raise RuntimeError(f"mysql detect error: {err.strip()}")
    db = out.strip().split("\n")[0].strip()
    if not db:
        raise RuntimeError("no Frappe DB with tabJob Applicant found")
    _hrms_db_name = db
    return db


def hrms_sql(sql: str) -> str:
    """Query HRMS MariaDB, return raw stdout."""
    db = _detect_hrms_db()
    rc, out, err = docker_exec(
        HRMS_DB_CONTAINER,
        "mysql", "-uroot", f"-p{os.environ.get('HRMS_DB_ROOT_PASSWORD', 'hrms123456')}",
        "--default-character-set=utf8mb4",
        db, "-N", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"mysql error: {err.strip()}")
    return out.strip()


_bc_db_name: str | None = None


def _detect_bigcapital_db() -> str:
    """Find the BigCapital tenant DB (has EXPENSES_TRANSACTIONS table)."""
    global _bc_db_name
    if _bc_db_name:
        return _bc_db_name
    rc, out, err = docker_exec(
        BIGCAPITAL_CONTAINER,
        "mysql", "-uroot", "-N", "-B", "-e",
        "SELECT TABLE_SCHEMA FROM information_schema.TABLES "
        "WHERE TABLE_NAME='EXPENSES_TRANSACTIONS' "
        "AND TABLE_SCHEMA LIKE 'bigcapital_tenant%' LIMIT 1",
    )
    if rc != 0:
        raise RuntimeError(f"bigcapital mysql detect error: {err.strip()}")
    db = out.strip().split("\n")[0].strip()
    if not db:
        raise RuntimeError("no bigcapital tenant DB with EXPENSES_TRANSACTIONS found")
    _bc_db_name = db
    return db


def bigcapital_sql(sql: str) -> str:
    """Query BigCapital tenant MariaDB, return raw stdout."""
    db = _detect_bigcapital_db()
    rc, out, err = docker_exec(
        BIGCAPITAL_CONTAINER,
        "mysql", "-uroot", "-N", "-B", "-D", db, "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"bigcapital mysql error: {err.strip()}")
    return out.strip()


def twenty_sql(sql: str) -> str:
    """Query Twenty Postgres, return raw stdout."""
    rc, out, err = docker_exec(
        TWENTY_DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "default", "-t", "-A", "-c", sql,
    )
    if rc != 0:
        raise RuntimeError(f"psql error: {err.strip()}")
    return out.strip()


def get_twenty_workspace_schema() -> str:
    """Find the Twenty workspace schema name."""
    result = twenty_sql(
        # The image seeds TWO workspaces (Apple/apple, YCombinator/yc). The app serves yc on the
        # benchmark's subdomain-less localhost URL, so that is where the agent's writes land; an
        # unordered LIMIT 1 only happened to agree, and ORDER BY schema_name picks Apple and
        # silently finds nothing. core."dataSource" carries the authoritative schema mapping.
        'SELECT ds.schema FROM core."dataSource" ds '
        'JOIN core.workspace w ON w.id = ds."workspaceId" '
        "WHERE w.subdomain = 'yc';"
    )
    if not result:
        raise RuntimeError("no workspace schema found")
    return result.split("\n")[0].strip()


def twenty_find_id(ws: str, table: str, title: str) -> str:
    """Locate a non-deleted record by exact title; return id ('' if absent)."""
    result = twenty_sql(
        f"SELECT id FROM \"{ws}\".{table} "
        f"WHERE title = '{title}' AND \"deletedAt\" IS NULL LIMIT 1"
    )
    return result.split("\n")[0].strip() if result else ""


def twenty_body(ws: str, table: str, rec_id: str) -> str:
    """Fetch bodyV2Markdown as a single column, newlines flattened SQL-side."""
    return twenty_sql(
        f"SELECT regexp_replace(COALESCE(\"bodyV2Markdown\", ''), "
        f"E'[\\n\\r]+', ' ', 'g') "
        f"FROM \"{ws}\".{table} WHERE id = '{rec_id}' AND \"deletedAt\" IS NULL"
    )


def twenty_due_at_utc(ws: str, table: str, rec_id: str) -> str:
    """Fetch dueAt rendered as a UTC 'YYYY-MM-DD HH:MI:SS' string ('' if NULL)."""
    return twenty_sql(
        f"SELECT to_char(\"dueAt\" AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') "
        f"FROM \"{ws}\".{table} WHERE id = '{rec_id}' AND \"deletedAt\" IS NULL"
    )


# ── BigCapital API helpers ────────────────────────────────────────────────────
_bc_token: str | None = None
_bc_org_id: str | None = None


def bigcapital_auth() -> tuple[str, str]:
    """Login to BigCapital, return (token, org_id). Cached."""
    global _bc_token, _bc_org_id
    if _bc_token and _bc_org_id:
        return _bc_token, _bc_org_id
    url = f"http://{HOST}:{BIGCAPITAL_PORT}/api/auth/signin"
    r = requests.post(url, json={
        "email": "admin@bigcapital.local",
        "password": "admin123",
    }, timeout=15)
    r.raise_for_status()
    data = r.json()
    _bc_token = data["access_token"]
    _bc_org_id = data.get("organization_id", "")
    return _bc_token, _bc_org_id


def bigcapital_get(path: str) -> dict:
    """GET a BigCapital API endpoint (auto-auth)."""
    token, org_id = bigcapital_auth()
    r = requests.get(
        f"http://{HOST}:{BIGCAPITAL_PORT}/api{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "organization-id": str(org_id),
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


# ── HRMS shared lookups ───────────────────────────────────────────────────────
ROUND_1 = "HR Analytical Skills Test"
ROUND_2 = "HR Director Final Round"
JOB_OPENING_SUBQ = (
    "(SELECT name FROM `tabJob Opening` "
    "WHERE designation='Business Analyst' AND department='Human Resources - TVS')"
)


def _applicant_id(email: str) -> str:
    result = hrms_sql(
        f"SELECT name FROM `tabJob Applicant` WHERE email_id='{email}'"
    )
    return result.split("\n")[0].strip() if result else ""


def _interviews(applicant_id: str, round_name: str) -> list[tuple[str, str, str, str]]:
    """(name, status, docstatus, scheduled_on) rows for one applicant+round."""
    result = hrms_sql(
        f"SELECT name, status, docstatus, scheduled_on FROM `tabInterview` "
        f"WHERE job_applicant='{applicant_id}' AND interview_round='{round_name}'"
    )
    rows = []
    for line in (result.split("\n") if result else []):
        parts = line.split("\t")
        if len(parts) >= 4:
            rows.append((parts[0].strip(), parts[1].strip(),
                         parts[2].strip(), parts[3].strip()))
    return rows


def _interviewers(interview_name: str) -> set[str]:
    result = hrms_sql(
        f"SELECT interviewer FROM `tabInterview Detail` "
        f"WHERE parent='{interview_name}'"
    )
    if not result:
        return set()
    return {line.strip() for line in result.split("\n")
            if line.strip() and line.strip().upper() != "NULL"}


def _feedback_rows(applicant_id: str, round_name: str) -> list[tuple[str, float]]:
    """(result, average_rating) for submitted feedback of one applicant+round."""
    result = hrms_sql(
        f"SELECT result, average_rating FROM `tabInterview Feedback` "
        f"WHERE job_applicant='{applicant_id}' "
        f"AND interview_round='{round_name}' AND docstatus=1"
    )
    rows = []
    for line in (result.split("\n") if result else []):
        parts = line.split("\t")
        if len(parts) >= 2:
            try:
                rating = float(parts[1].strip())
            except ValueError:
                rating = -1.0
            rows.append((parts[0].strip(), rating))
    return rows


def _ananya_users() -> set[str]:
    result = hrms_sql("SELECT name FROM `tabUser` WHERE full_name='Ananya Reddy'")
    if not result:
        return set()
    return {line.strip() for line in result.split("\n") if line.strip()}


# ── HRMS Checks ───────────────────────────────────────────────────────────────

def check_1_job_requisition() -> None:
    """Job requisition submitted for Business Analyst / HR - TVS with 1 position,
    expected compensation 1100000, and the required justification text."""
    try:
        result = hrms_sql(
            "SELECT name, docstatus, status, no_of_positions, expected_compensation "
            "FROM `tabJob Requisition` "
            "WHERE designation='Business Analyst' "
            "AND department='Human Resources - TVS' "
            "AND company='TechVista Solutions Pvt. Ltd.'"
        )
        if not result:
            check("1. Job requisition submitted", 1, False, "not found")
            return
        row = result.split("\n")[0].split("\t")
        name = row[0].strip()
        docstatus = row[1].strip() if len(row) > 1 else ""
        status = row[2].strip() if len(row) > 2 else ""
        positions = row[3].strip() if len(row) > 3 else ""
        compensation = row[4].strip() if len(row) > 4 else ""
        # Job Requisition is not a submittable DocType in this HRMS instance
        # (is_submittable=0), so docstatus can never reach 1; the workflow
        # equivalent of "submitted" is an approved/open status.
        submitted = docstatus == "1" or status in ("Open & Approved", "Approved")
        try:
            positions_ok = float(positions) == 1
        except ValueError:
            positions_ok = False
        try:
            comp_ok = abs(float(compensation) - 1100000) < 0.01
        except ValueError:
            comp_ok = False
        desc = hrms_sql(
            f"SELECT description FROM `tabJob Requisition` WHERE name='{name}'"
        )
        desc_ok = _contains(desc, "data-driven workforce planning")
        passed = submitted and positions_ok and comp_ok and desc_ok
        check("1. Job requisition submitted", 1, passed,
              f"docstatus={docstatus} status={status} positions={positions} "
              f"compensation={compensation} desc_ok={desc_ok}")
    except Exception as e:
        check("1. Job requisition submitted", 1, False, f"exception: {e}")


def check_2_job_opening() -> None:
    """Job opening for Business Analyst, status Open, 1 position, required description"""
    try:
        result = hrms_sql(
            "SELECT name, status, vacancies "
            "FROM `tabJob Opening` "
            "WHERE designation='Business Analyst' "
            "AND department='Human Resources - TVS'"
        )
        if not result:
            check("2. Job opening Open with 1 position", 1, False, "not found")
            return
        row = result.split("\n")[0].split("\t")
        name = row[0].strip()
        status = row[1].strip() if len(row) > 1 else ""
        vacancies = row[2].strip() if len(row) > 2 else ""
        desc = hrms_sql(
            f"SELECT description FROM `tabJob Opening` WHERE name='{name}'"
        )
        desc_ok = (_contains(desc, "detail-oriented Business Analyst")
                   and _contains(desc, "Power BI"))
        passed = status == "Open" and str(vacancies) == "1" and desc_ok
        check("2. Job opening Open with 1 position", 1, passed,
              f"status={status}, vacancies={vacancies}, desc_ok={desc_ok}")
    except Exception as e:
        check("2. Job opening Open with 1 position", 1, False, f"exception: {e}")


def _check_applicant(label: str, email: str, exp_name: str,
                     exp_status: str, exp_source: str) -> None:
    """Applicant status + name + source + link to the Business Analyst opening."""
    try:
        result = hrms_sql(
            f"SELECT status, applicant_name, source, "
            f"(job_title IN {JOB_OPENING_SUBQ}) "
            f"FROM `tabJob Applicant` WHERE email_id='{email}'"
        )
        if not result:
            check(label, 1, False, "applicant not found")
            return
        row = result.split("\n")[0].split("\t")
        status = row[0].strip()
        applicant_name = row[1].strip() if len(row) > 1 else ""
        source = row[2].strip() if len(row) > 2 else ""
        linked = (row[3].strip() if len(row) > 3 else "") == "1"
        passed = (status == exp_status and applicant_name == exp_name
                  and source == exp_source and linked)
        check(label, 1, passed,
              f"status={status!r} name={applicant_name!r} source={source!r} "
              f"linked_to_opening={linked}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_3_karan_accepted() -> None:
    _check_applicant("3. Karan Mehta status Accepted",
                     "karan.mehta@gmail.com", "Karan Mehta", "Accepted", "LinkedIn")


def check_4_divya_open() -> None:
    _check_applicant("4. Divya Pillai status Open",
                     "divya.pillai@outlook.com", "Divya Pillai", "Open",
                     "Employee Referral")


def check_5_rohit_rejected() -> None:
    _check_applicant("5. Rohit Nambiar status Rejected",
                     "rohit.nambiar@yahoo.com", "Rohit Nambiar", "Rejected", "Indeed")


def check_6_interview_rounds() -> None:
    """Both interview rounds exist"""
    try:
        result = hrms_sql(
            "SELECT name FROM `tabInterview Round` "
            "WHERE name IN ('HR Analytical Skills Test','HR Director Final Round')"
        )
        found = set(line.strip() for line in result.split("\n")) if result else set()
        has_r1 = "HR Analytical Skills Test" in found
        has_r2 = "HR Director Final Round" in found
        check("6. Interview rounds exist", 1, has_r1 and has_r2,
              f"round1={has_r1}, round2={has_r2}")
    except Exception as e:
        check("6. Interview rounds exist", 1, False, f"exception: {e}")


def _check_round(label: str, weight: int, round_name: str, exp_date: str,
                 people: list[tuple[str, str, str, float, str | None]]) -> None:
    """Per-person round check: submitted interview on the right date with the
    right status; required interviewer (hard gate when given, diagnostic-only
    for the Ananya Reddy case); submitted feedback with result + rating."""
    try:
        issues: list[str] = []
        diags: list[str] = []
        ananya = _ananya_users()
        for name, email, exp_status, exp_rating, req_interviewer in people:
            aid = _applicant_id(email)
            if not aid:
                issues.append(f"{name}: applicant not found")
                continue
            rows = _interviews(aid, round_name)
            ok_rows = [r for r in rows
                       if r[3][:10] == exp_date and r[2] == "1"
                       and r[1] == exp_status]
            if not ok_rows:
                issues.append(
                    f"{name}: no submitted interview on {exp_date} with "
                    f"status {exp_status} (rows={rows})")
            else:
                interviewers: set[str] = set()
                for r in ok_rows:
                    interviewers |= _interviewers(r[0])
                if req_interviewer is not None:
                    if req_interviewer not in interviewers:
                        issues.append(
                            f"{name}: interviewer {req_interviewer} missing "
                            f"(found {sorted(interviewers)})")
                else:
                    # Expected interviewer is Ananya Reddy, who has no seed
                    # User record (Link->User unfillable) -> diagnostic only.
                    if ananya and (interviewers & ananya):
                        diags.append(f"{name}: interviewer=Ananya Reddy ok")
                    else:
                        diags.append(
                            f"{name}: interviewer not gated (found "
                            f"{sorted(interviewers)}, Ananya user "
                            f"{'exists' if ananya else 'absent'})")
            feedback = _feedback_rows(aid, round_name)
            if not any(res == exp_status and abs(rating - exp_rating) <= 0.01
                       for res, rating in feedback):
                issues.append(
                    f"{name}: no submitted feedback result={exp_status} "
                    f"rating~{exp_rating} (found {feedback})")
        detail = "; ".join(issues) if issues else "all correct"
        if diags:
            detail += " | " + "; ".join(diags)
        check(label, weight, not issues, detail)
    except Exception as e:
        check(label, weight, False, f"exception: {e}")


def check_7_round1_feedback() -> None:
    """Round 1: 3 submitted interviews on 2026-08-05 with correct statuses,
    interviewers, and submitted feedback ratings (5/5, 4/5, 2/5)."""
    _check_round("7. Round 1 feedback", 2, ROUND_1, "2026-08-05", [
        ("Karan Mehta", "karan.mehta@gmail.com", "Cleared", 1.0, None),
        ("Divya Pillai", "divya.pillai@outlook.com", "Cleared", 0.8, None),
        ("Rohit Nambiar", "rohit.nambiar@yahoo.com", "Rejected", 0.4,
         "pooja.malhotra@techvista.com"),
    ])


def check_8_round2_feedback() -> None:
    """Round 2: Karan and Divya submitted interviews on 2026-08-12 with
    Rajesh Kumar and feedback ratings 5/5 and 4/5, both Cleared."""
    _check_round("8. Round 2 feedback", 2, ROUND_2, "2026-08-12", [
        ("Karan Mehta", "karan.mehta@gmail.com", "Cleared", 1.0,
         "rajesh.kumar@techvista.com"),
        ("Divya Pillai", "divya.pillai@outlook.com", "Cleared", 0.8,
         "rajesh.kumar@techvista.com"),
    ])


def check_9_job_offer() -> None:
    """Submitted job offer for Karan Mehta dated 2026-08-19 with designation
    Business Analyst and a salary term containing 1050000."""
    try:
        applicant_id = _applicant_id("karan.mehta@gmail.com")
        if not applicant_id:
            check("9. Job offer for Karan Mehta", 2, False, "applicant not found")
            return
        result = hrms_sql(
            f"SELECT name, designation, offer_date, docstatus "
            f"FROM `tabJob Offer` "
            f"WHERE job_applicant='{applicant_id}'"
        )
        if not result:
            check("9. Job offer for Karan Mehta", 2, False, "no job offer found")
            return
        row = result.split("\n")[0].split("\t")
        offer_name = row[0].strip()
        designation = row[1].strip() if len(row) > 1 else ""
        offer_date = row[2].strip() if len(row) > 2 else ""
        docstatus = row[3].strip() if len(row) > 3 else ""
        # Salary term: some row whose value contains 1050000 (commas stripped)
        term_count = hrms_sql(
            f"SELECT COUNT(*) FROM `tabJob Offer Term` "
            f"WHERE parent='{offer_name}' "
            f"AND REPLACE(value, ',', '') LIKE '%1050000%'"
        )
        has_salary = term_count.isdigit() and int(term_count) > 0
        has_designation = designation == "Business Analyst"
        date_ok = offer_date[:10] == "2026-08-19"
        submitted = docstatus == "1"
        passed = has_salary and has_designation and date_ok and submitted
        check("9. Job offer for Karan Mehta", 2, passed,
              f"designation={designation}, offer_date={offer_date}, "
              f"docstatus={docstatus}, salary_in_terms={has_salary}")
    except Exception as e:
        check("9. Job offer for Karan Mehta", 2, False, f"exception: {e}")


# ── BigCapital Checks ────────────────────────────────────────────────────────

def check_10_expense_account() -> None:
    """HR Recruitment Cost Account exists as Expense type"""
    try:
        data = bigcapital_get("/accounts?search=HR+Recruitment+Cost+Account")
        accounts = data.get("accounts", [])
        found = None
        for a in accounts:
            if a.get("name") == "HR Recruitment Cost Account":
                found = a
                break
        if not found:
            check("10. Expense account exists", 1, False, "account not found")
            return
        acct_type = found.get("account_type", "")
        passed = acct_type.lower() == "expense"
        check("10. Expense account exists", 1, passed, f"type={acct_type}")
    except Exception as e:
        check("10. Expense account exists", 1, False, f"exception: {e}")


def check_11_expenses() -> None:
    """Exactly two published Petty Cash expenses under HR Recruitment Cost
    Account with same-row date/amount/reference binding, plus GL debits."""
    try:
        raw = bigcapital_sql(
            "SELECT DATE(E.PAYMENT_DATE), PA.NAME, C.AMOUNT, "
            "(E.PUBLISHED_AT IS NOT NULL), E.REFERENCE_NO "
            "FROM EXPENSES_TRANSACTIONS E "
            "JOIN ACCOUNTS PA ON PA.ID = E.PAYMENT_ACCOUNT_ID "
            "JOIN EXPENSE_TRANSACTION_CATEGORIES C ON C.EXPENSE_ID = E.ID "
            "JOIN ACCOUNTS EA ON EA.ID = C.EXPENSE_ACCOUNT_ID "
            "WHERE EA.NAME = 'HR Recruitment Cost Account'"
        )
        rows = []
        for line in (raw.split("\n") if raw else []):
            parts = line.split("\t", 4)
            if len(parts) >= 5:
                pay_date, pa_name, amount, published, reference = parts
                try:
                    amt = float(amount.strip())
                except ValueError:
                    amt = -1.0
                rows.append((pay_date.strip(), pa_name.strip(), amt,
                             published.strip(), reference.strip()))
        issues = []
        if len(rows) != 2:
            issues.append(f"expected exactly 2 expense rows, found {len(rows)}")
        specs = [
            ("2026-08-07", 4800.0, "recruiter fee"),
            ("2026-08-13", 650.0, "job board posting"),
        ]
        matched: set[int] = set()
        for exp_date, exp_amt, ref_phrase in specs:
            hit = None
            for idx, (pay_date, pa_name, amt, published, reference) in enumerate(rows):
                if idx in matched:
                    continue
                if (pay_date == exp_date and abs(amt - exp_amt) < 0.01
                        and pa_name == "Petty Cash" and published == "1"
                        and _contains(reference, ref_phrase)):
                    hit = idx
                    break
            if hit is None:
                issues.append(
                    f"no published Petty Cash row ({exp_date}, {exp_amt:g}, "
                    f"ref~{ref_phrase!r})")
            else:
                matched.add(hit)
        # GL posting: one debit per date, sum 5450
        gl_raw = bigcapital_sql(
            "SELECT DATE(T.DATE), T.DEBIT "
            "FROM ACCOUNTS_TRANSACTIONS T "
            "JOIN ACCOUNTS A ON A.ID = T.ACCOUNT_ID "
            "WHERE A.NAME = 'HR Recruitment Cost Account' AND T.DEBIT > 0"
        )
        gl_rows = []
        for line in (gl_raw.split("\n") if gl_raw else []):
            parts = line.split("\t")
            if len(parts) >= 2:
                try:
                    debit = float(parts[1].strip())
                except ValueError:
                    debit = -1.0
                gl_rows.append((parts[0].strip(), debit))
        n_4800 = sum(1 for d, a in gl_rows
                     if d == "2026-08-07" and abs(a - 4800) < 0.01)
        n_650 = sum(1 for d, a in gl_rows
                    if d == "2026-08-13" and abs(a - 650) < 0.01)
        gl_total = sum(a for _, a in gl_rows)
        if n_4800 != 1:
            issues.append(f"GL debit 4800 on 2026-08-07: {n_4800} rows")
        if n_650 != 1:
            issues.append(f"GL debit 650 on 2026-08-13: {n_650} rows")
        if abs(gl_total - 5450) >= 0.01:
            issues.append(f"GL debit total {gl_total:g} != 5450")
        check("11. Expense entries total 5450", 2, not issues,
              "; ".join(issues) if issues else
              f"rows={rows}, gl_total={gl_total:g}")
    except Exception as e:
        check("11. Expense entries total 5450", 2, False, f"exception: {e}")


# ── Twenty CRM Checks (docker exec Postgres) ─────────────────────────────────

def check_12_onboarding_task() -> None:
    """Task 'Onboard Karan Mehta - Business Analyst': due 2026-09-01 and body
    covering offer/salary/costs/date/department."""
    try:
        ws = get_twenty_workspace_schema()
        task_id = twenty_find_id(ws, "task", "Onboard Karan Mehta - Business Analyst")
        if not task_id:
            check("12. Onboarding task", 2, False, "task not found")
            return
        due = twenty_due_at_utc(ws, "task", task_id)
        due_ok = date_matches_tz(due, "2026-09-01")
        body = twenty_body(ws, "task", task_id)
        nb = _norm(body)
        has_offer = "offer accepted" in nb.lower()
        has_salary = "1050000" in nb
        has_cost = "5450" in nb
        has_4800 = "4800" in nb
        has_650 = "650" in nb
        has_date = "2026-08-19" in nb
        has_dept = _contains(body, "Human Resources - TVS")
        passed = (due_ok and has_offer and has_salary and has_cost
                  and has_4800 and has_650 and has_date and has_dept)
        check("12. Onboarding task", 2, passed,
              f"dueAt={due!r} due_ok={due_ok}, offer_accepted={has_offer}, "
              f"salary={has_salary}, cost={has_cost}, 4800={has_4800}, "
              f"650={has_650}, offer_date={has_date}, dept={has_dept}")
    except Exception as e:
        check("12. Onboarding task", 2, False, f"exception: {e}")


def check_13_rejection_task() -> None:
    """Task 'Send rejection notifications - Business Analyst recruitment':
    due 2026-08-21, body names both candidates and their ratings."""
    try:
        ws = get_twenty_workspace_schema()
        task_id = twenty_find_id(
            ws, "task",
            "Send rejection notifications - Business Analyst recruitment")
        if not task_id:
            check("13. Rejection notification task", 2, False, "task not found")
            return
        due = twenty_due_at_utc(ws, "task", task_id)
        due_ok = date_matches_tz(due, "2026-08-21")
        body = twenty_body(ws, "task", task_id)
        nb = _norm(body)
        has_rohit = "rohit.nambiar@yahoo.com" in nb.lower()
        has_divya = "divya pillai" in nb.lower()
        has_2of5 = "2/5" in nb
        has_4of5 = "4/5" in nb
        passed = due_ok and has_rohit and has_divya and has_2of5 and has_4of5
        check("13. Rejection notification task", 2, passed,
              f"dueAt={due!r} due_ok={due_ok}, has_rohit={has_rohit}, "
              f"has_divya={has_divya}, 2/5={has_2of5}, 4/5={has_4of5}")
    except Exception as e:
        check("13. Rejection notification task", 2, False, f"exception: {e}")


def check_14_recruitment_note() -> None:
    """Note 'Recruitment Summary - Business Analyst - 2026-08-19' with full
    applicant/outcome/cost content."""
    try:
        ws = get_twenty_workspace_schema()
        note_id = twenty_find_id(
            ws, "note", "Recruitment Summary - Business Analyst - 2026-08-19")
        if not note_id:
            check("14. Recruitment summary note", 2, False, "note not found")
            return
        body = twenty_body(ws, "note", note_id)
        nb = _norm(body)
        nb_lower = nb.lower()
        required_ci = [  # case-insensitive (emails)
            "karan.mehta@gmail.com",
            "divya.pillai@outlook.com",
            "rohit.nambiar@yahoo.com",
        ]
        required_cs = [  # case-sensitive tokens
            "ACCEPTED", "Waitlisted", "REJECTED", "5450",
            "LinkedIn", "Employee Referral", "Indeed",
            "5/5", "2/5", "4800", "650",
        ]
        missing = [t for t in required_ci if t not in nb_lower]
        missing += [t for t in required_cs if t not in nb]
        if not _contains(body, "HR Recruitment Cost Account"):
            missing.append("HR Recruitment Cost Account")
        check("14. Recruitment summary note", 2, not missing,
              "all content present" if not missing
              else f"missing={missing}")
    except Exception as e:
        check("14. Recruitment summary note", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_job_requisition()
    check_2_job_opening()
    check_3_karan_accepted()
    check_4_divya_open()
    check_5_rohit_rejected()
    check_6_interview_rounds()
    check_7_round1_feedback()
    check_8_round2_feedback()
    check_9_job_offer()
    check_10_expense_account()
    check_11_expenses()
    check_12_onboarding_task()
    check_13_rejection_task()
    check_14_recruitment_note()

    total = sum(w for _, w, _, _ in _checks)
    earned = sum(w for _, w, p, _ in _checks if p)
    all_pass = all(p for _, _, p, _ in _checks) and bool(_checks)
    score = (earned / total) if total else 0.0

    print(
        f"SCORE: {score:.3f}  PASS: {all_pass}  ({earned}/{total})",
        file=sys.stderr,
    )
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
