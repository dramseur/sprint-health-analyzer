#!/usr/bin/env python3
"""
Sprint Health Analyzer
======================
Generates comprehensive Agile sprint health reports from Jira CSV exports.

Usage:
    # Analyze a CSV file (basic analysis from CSV data only)
    python3 sprint_health_analyzer.py --csv "sprint.csv" --sprint "Sprint NN" --team "Team Name"

    # Analyze directly from Jira using sprint ID
    python3 sprint_health_analyzer.py --sprintid 12345 --jira-url https://issues.redhat.com --jira-user user@example.com --jira-token TOKEN

    # Same, using environment variables for credentials
    export JIRA_URL=https://issues.redhat.com JIRA_USER=user@example.com JIRA_TOKEN=TOKEN
    python3 sprint_health_analyzer.py --sprintid 12345

    # Analyze CSV with sprint ID context
    python3 sprint_health_analyzer.py --csv "sprint.csv" --sprintid 12345 --team "Team Name"

    # Analyze from Jira MCP tool JSON output (no CSV or API credentials needed)
    python3 sprint_health_analyzer.py --jira-json "mcp_output.json" --sprint "Sprint NN" --team "Team Name"

    # Analyze with Jira enrichment data
    python3 sprint_health_analyzer.py --csv "sprint.csv" --sprint "Sprint NN" --team "Team Name" --enrichment enrichment.json

    # Generate enrichment request list (issue keys to look up in Jira)
    python3 sprint_health_analyzer.py --csv "sprint.csv" --sprint "Sprint NN" --team "Team Name" --enrichment-requests

    # Specify output directory
    python3 sprint_health_analyzer.py --csv "sprint.csv" --sprint "Sprint NN" --team "Team Name" --output /path/to/output/

Inputs:
    - CSV: Standard Jira CSV export (any number of columns; auto-detects key fields)
    - MCP JSON: Raw JSON output from Jira MCP tools (jira_get_sprint_issues / jira_search)
    - Enrichment JSON (optional): Changelog/comment data gathered from Jira API

Outputs:
    - {Sprint}_Health_Report.md   -- Markdown report
    - {Sprint}_Health_Report.html -- Styled HTML report
    - enrichment_requests.json    -- (optional) List of issue keys needing Jira lookup
"""

import csv
import json
import sys
import os
import re
import argparse
import base64
from datetime import datetime, timedelta
from collections import defaultdict, Counter
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

# ---------------------------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------------------------
TODAY = datetime.now()
REPORT_DATE = TODAY.strftime("%B %d, %Y")

STATUS_ORDER = {
    "Resolved": 0, "Closed": 0, "Done": 0,
    "Review": 1, "Code Review": 1, "In Review": 1, "Peer Review": 1,
    "Testing": 2, "QE Review": 2, "In Testing": 2, "QA": 2,
    "In Progress": 3, "Development": 3, "Active": 3,
    "New": 4, "Open": 4, "To Do": 4, "Backlog": 4, "Refinement": 4,
}

STATUS_CATEGORY = {
    "Resolved": "done", "Closed": "done", "Done": "done",
    "Review": "review", "Code Review": "review", "In Review": "review", "Peer Review": "review",
    "Testing": "testing", "QE Review": "testing", "In Testing": "testing", "QA": "testing",
    "In Progress": "inprogress", "Development": "inprogress", "Active": "inprogress",
    "New": "new", "Open": "new", "To Do": "new", "Backlog": "new", "Refinement": "new",
}

def status_cat(status):
    """Map a status string to a category."""
    return STATUS_CATEGORY.get(status, "new")

def is_done(status):
    return status_cat(status) == "done"


# ---------------------------------------------------------------------------
# CSV Parsing
# ---------------------------------------------------------------------------

def find_column(headers, keywords, exact_match=None):
    """Find a column index by keyword matching. Returns the index or -1."""
    if exact_match:
        for i, h in enumerate(headers):
            if h.strip() == exact_match:
                return i
    for i, h in enumerate(headers):
        hl = h.lower().strip()
        if all(k.lower() in hl for k in keywords):
            return i
    return -1


def find_all_columns(headers, name):
    """Find all column indices matching an exact header name."""
    return [i for i, h in enumerate(headers) if h.strip() == name]


def parse_date(s):
    """Parse a date string in various Jira export formats."""
    if not s or not s.strip():
        return None
    s = s.strip()
    # Try common formats
    for fmt in [
        "%Y/%m/%d %I:%M %p",   # 2026/03/03 11:18 AM
        "%Y-%m-%dT%H:%M:%S.%f%z",  # ISO format with tz
        "%Y-%m-%dT%H:%M:%S.%f",    # ISO format without tz (after +zone strip)
        "%Y-%m-%dT%H:%M:%S",       # ISO format no fractional
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d/%b/%y %I:%M %p",   # 03/Mar/26 11:18 AM
        "%d/%b/%Y %I:%M %p",
        "%m/%d/%Y",
        "%d/%m/%Y",
    ]:
        try:
            return datetime.strptime(s.split("+")[0].strip(), fmt)
        except ValueError:
            continue
    # Last resort: try dateutil
    try:
        from dateutil.parser import parse as du_parse
        return du_parse(s)
    except Exception:
        return None


def parse_points(val):
    """Parse story points from a string. Returns float or 0."""
    if not val or not val.strip():
        return None  # Distinguish "no estimate" from "0"
    try:
        return float(val.strip())
    except ValueError:
        return None


def extract_sprint_number(sprint_str, team_prefix=None):
    """Extract sprint number from a sprint name like 'Training Kubeflow Sprint 26'."""
    if not sprint_str:
        return None
    # Try to find "Sprint XX" pattern
    m = re.search(r'Sprint\s*(\d+\w*)', sprint_str, re.IGNORECASE)
    if m:
        return f"S{m.group(1)}"
    # Fallback: last word/number
    parts = sprint_str.strip().split()
    if parts:
        return f"S{parts[-1]}"
    return sprint_str


def has_acceptance_criteria(text):
    """Detect whether a description contains acceptance criteria.

    Scans the full text (not truncated) for common AC patterns:
    - Explicit headers: "Acceptance Criteria", "Definition of Done", "AC:", "DoD"
    - Structured criteria: "Done when", "Success criteria", "Exit criteria"
    - Test-oriented: "Acceptance test", "Test case", "Given/When/Then"
    - Checklist patterns: markdown checkboxes "- [ ]" or "- [x]"
    """
    if not text or not text.strip():
        return False
    return bool(re.search(
        r'acceptance\s*criteria'
        r'|definition\s*of\s*done'
        r'|done\s*when'
        r'|acceptance\s*test'
        r'|success\s*criteria'
        r'|exit\s*criteria'
        r'|\bDoD\b'
        r'|\bAC\s*:'
        r'|\bgiven\b.*\bwhen\b.*\bthen\b'
        r'|-\s*\[\s*[xX ]?\s*\]',
        text, re.IGNORECASE
    ))


def parse_csv(filepath, target_sprint=None):
    """
    Parse a Jira CSV export into a list of item dicts.

    Args:
        filepath: Path to the CSV file
        target_sprint: Name or number of the sprint to analyze (e.g., "Sprint 26" or "26").
                       If None, analyzes all items in the CSV.

    Returns:
        tuple: (items_list, team_name, sprint_name)
    """
    items = []

    with open(filepath, encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        headers = next(reader)

    # Identify key columns
    col_map = {}
    col_map['summary'] = find_column(headers, ['summary'], exact_match='Summary')
    col_map['key'] = find_column(headers, ['issue key'], exact_match='Issue key')
    col_map['issue_id'] = find_column(headers, ['issue id'], exact_match='Issue id')
    col_map['type'] = find_column(headers, ['issue type'], exact_match='Issue Type')
    col_map['status'] = find_column(headers, ['status'], exact_match='Status')
    col_map['priority'] = find_column(headers, ['priority'], exact_match='Priority')
    col_map['assignee'] = find_column(headers, ['assignee'], exact_match='Assignee')
    col_map['reporter'] = find_column(headers, ['reporter'], exact_match='Reporter')
    col_map['created'] = find_column(headers, ['created'], exact_match='Created')
    col_map['updated'] = find_column(headers, ['updated'], exact_match='Updated')
    col_map['resolved'] = find_column(headers, ['resolved'], exact_match='Resolved')
    col_map['description'] = find_column(headers, ['description'], exact_match='Description')

    # Story Points -- try multiple column names
    sp_idx = find_column(headers, ['story points'], exact_match='Custom field (Story Points)')
    if sp_idx == -1:
        sp_idx = find_column(headers, ['story points'], exact_match='Story Points')
    if sp_idx == -1:
        sp_idx = find_column(headers, ['story', 'points'])
    col_map['points'] = sp_idx

    # Acceptance Criteria
    ac_idx = find_column(headers, ['acceptance criteria'], exact_match='Custom field (Acceptance Criteria)')
    if ac_idx == -1:
        ac_idx = find_column(headers, ['acceptance', 'criteria'])
    col_map['ac'] = ac_idx

    # Sprint columns (may be multiple)
    sprint_indices = find_all_columns(headers, 'Sprint')

    # Blocker links
    block_indices = [i for i, h in enumerate(headers) if 'block' in h.lower() and 'link' in h.lower()]

    # Clone links
    clone_indices = [i for i, h in enumerate(headers) if 'clone' in h.lower() and 'link' in h.lower()]

    # Detect team name and sprint name from Sprint column values
    detected_team = None
    detected_sprint = None

    with open(filepath, encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        next(reader)  # skip headers

        for row in reader:
            if len(row) < max(v for v in col_map.values() if v >= 0) + 1:
                continue

            def val(col_name):
                idx = col_map.get(col_name, -1)
                if idx < 0 or idx >= len(row):
                    return ""
                return row[idx].strip()

            # Collect sprint names from all Sprint columns
            sprints_raw = []
            for si in sprint_indices:
                if si < len(row) and row[si].strip():
                    sprints_raw.append(row[si].strip())

            # Detect team/sprint from first non-empty sprint value
            if not detected_team and sprints_raw:
                for sr in sprints_raw:
                    m = re.match(r'^(.+?)\s*[-–]\s*Sprint\s*(\S+)', sr, re.IGNORECASE)
                    if not m:
                        m = re.match(r'^(.+?)\s+Sprint\s*(\S+)', sr, re.IGNORECASE)
                    if m:
                        detected_team = m.group(1).strip()
                        break

            # Determine if this item is in the target sprint
            sprint_labels = [extract_sprint_number(s) for s in sprints_raw]

            if target_sprint:
                # Normalize target sprint
                target_norm = target_sprint.strip()
                target_num = re.search(r'(\d+\w*)', target_norm)
                target_snum = f"S{target_num.group(1)}" if target_num else target_norm

                in_target = target_snum in sprint_labels
                if not in_target:
                    # Also try matching full sprint name
                    in_target = any(target_norm.lower() in s.lower() for s in sprints_raw)

                if not in_target:
                    continue

                if not detected_sprint:
                    # Find the full sprint name matching the target
                    for sr in sprints_raw:
                        if target_norm.lower() in sr.lower() or target_snum in extract_sprint_number(sr, None):
                            detected_sprint = sr
                            break
            else:
                if not detected_sprint and sprints_raw:
                    detected_sprint = sprints_raw[-1]  # Use the last sprint

            # Parse fields
            created_dt = parse_date(val('created'))
            resolved_dt = parse_date(val('resolved'))
            points_val = parse_points(val('points'))

            # Calculate age
            age_days = (TODAY - created_dt).days if created_dt else 0

            # Calculate cycle time (for resolved items)
            # cycle_days is set later in compute_metrics using sprint start date
            cycle_days = None
            if resolved_dt and created_dt:
                cycle_days = (resolved_dt - created_dt).days  # Preliminary: total age, refined in compute_metrics

            # Check for AC content
            ac_text = val('ac')
            has_ac = bool(ac_text and ac_text.strip())

            # Check description for acceptance criteria (scan full text before truncating)
            desc_text = val('description')
            has_ac_in_desc = has_acceptance_criteria(desc_text)

            # Collect blocker links
            blockers = []
            for bi in block_indices:
                if bi < len(row) and row[bi].strip():
                    blockers.append(row[bi].strip())

            # Collect clone links
            clones = []
            for ci in clone_indices:
                if ci < len(row) and row[ci].strip():
                    clones.append(row[ci].strip())

            status = val('status')
            priority = val('priority')
            assignee = val('assignee')

            item = {
                'key': val('key'),
                'summary': val('summary'),
                'type': val('type'),
                'status': status,
                'status_cat': status_cat(status),
                'priority': priority,
                'priority_defined': bool(priority and priority.lower() not in ['undefined', '', 'none', 'trivial']),
                'assignee': assignee,
                'reporter': val('reporter'),
                'created': created_dt,
                'resolved': resolved_dt,
                'points': points_val if points_val is not None else 0,
                'has_estimate': points_val is not None,
                'has_ac': has_ac,
                'has_ac_in_desc': has_ac_in_desc,
                'age_days': age_days,
                'cycle_days': cycle_days,
                'sprints_raw': sprints_raw,
                'sprint_labels': sprint_labels,
                'sprint_count': len(sprint_labels),
                'blockers': blockers,
                'clones': clones,
                'description': desc_text[:500] if desc_text else '',
            }
            items.append(item)

    return items, detected_team, detected_sprint


# ---------------------------------------------------------------------------
# Jira REST API Fetch
# ---------------------------------------------------------------------------

def jira_api_get(url, user, token):
    """Make an authenticated GET request to Jira REST API."""
    auth_str = base64.b64encode(f"{user}:{token}".encode()).decode()
    req = Request(url, headers={
        'Authorization': f'Basic {auth_str}',
        'Accept': 'application/json',
    })
    with urlopen(req) as resp:
        return json.loads(resp.read().decode())


def fetch_sprint_issues(sprint_id, jira_url, jira_user, jira_token):
    """
    Fetch all issues in a sprint from Jira REST API.

    Returns:
        tuple: (items_list, team_name, sprint_name)
    """
    base = jira_url.rstrip('/')

    # Get sprint info
    sprint_info_url = f"{base}/rest/agile/1.0/sprint/{sprint_id}"
    try:
        sprint_data = jira_api_get(sprint_info_url, jira_user, jira_token)
    except HTTPError as e:
        print(f"ERROR: Failed to fetch sprint {sprint_id}: {e.code} {e.reason}")
        sys.exit(1)
    except URLError as e:
        print(f"ERROR: Cannot connect to Jira at {base}: {e.reason}")
        sys.exit(1)

    sprint_name = sprint_data.get('name', f'Sprint {sprint_id}')
    print(f"Sprint: {sprint_name} (state: {sprint_data.get('state', 'unknown')})")

    # Detect team name from sprint name
    detected_team = None
    m = re.match(r'^(.+?)\s*[-–]\s*Sprint\s*(\S+)', sprint_name, re.IGNORECASE)
    if not m:
        m = re.match(r'^(.+?)\s+Sprint\s*(\S+)', sprint_name, re.IGNORECASE)
    if m:
        detected_team = m.group(1).strip()

    # Fetch all issues in the sprint (paginated)
    items = []
    start_at = 0
    max_results = 50

    while True:
        issues_url = (
            f"{base}/rest/agile/1.0/sprint/{sprint_id}/issue"
            f"?startAt={start_at}&maxResults={max_results}"
            f"&fields=summary,issuetype,status,priority,assignee,reporter,"
            f"created,updated,resolutiondate,description,"
            f"customfield_10028,customfield_10020,customfield_10011,"
            f"customfield_10016,customfield_10004,"
            f"labels,issuelinks,parent"
        )
        data = jira_api_get(issues_url, jira_user, jira_token)
        issues = data.get('issues', [])

        for issue in issues:
            fields = issue.get('fields', {})

            # Parse dates
            created_dt = parse_date(fields.get('created', ''))
            resolved_dt = parse_date(fields.get('resolutiondate', ''))

            # Story points -- try Cloud field IDs (customfield_10028 is Red Hat Cloud)
            points_val = None
            for sp_field in ['customfield_10028', 'customfield_10016',
                             'customfield_10004', 'story_points']:
                sp = fields.get(sp_field)
                if sp is not None:
                    try:
                        points_val = float(sp)
                    except (ValueError, TypeError):
                        continue
                    break

            # Status
            status_obj = fields.get('status', {})
            status = status_obj.get('name', '') if status_obj else ''

            # Priority
            priority_obj = fields.get('priority', {})
            priority = priority_obj.get('name', '') if priority_obj else ''

            # Assignee / Reporter
            assignee_obj = fields.get('assignee', {})
            assignee = assignee_obj.get('displayName', '') if assignee_obj else ''
            reporter_obj = fields.get('reporter', {})
            reporter = reporter_obj.get('displayName', '') if reporter_obj else ''

            # Issue type
            type_obj = fields.get('issuetype', {})
            issue_type = type_obj.get('name', '') if type_obj else ''

            # Description
            desc_text = fields.get('description', '') or ''
            if isinstance(desc_text, dict):
                # ADF format -- extract text content
                desc_text = json.dumps(desc_text)

            # Check for acceptance criteria (scan full text before truncating)
            has_ac_in_desc = has_acceptance_criteria(desc_text)
            desc_text = desc_text[:500]

            # Sprint history -- customfield_10020 (Cloud), fallback to Agile API fields
            sprints_raw = []
            sprint_field_data = fields.get('customfield_10020')
            if sprint_field_data and isinstance(sprint_field_data, list):
                for s in sprint_field_data:
                    s_name = s.get('name', '') if isinstance(s, dict) else str(s)
                    if s_name:
                        sprints_raw.append(s_name)
            if not sprints_raw:
                # Fallback: Agile API sprint/closedSprints fields
                sprints_raw = [sprint_name]
                closed_sprints = fields.get('closedSprints', [])
                if isinstance(closed_sprints, list):
                    for cs in closed_sprints:
                        cs_name = cs.get('name', '') if isinstance(cs, dict) else str(cs)
                        if cs_name and cs_name != sprint_name:
                            sprints_raw.insert(0, cs_name)
            sprint_labels = [extract_sprint_number(s) for s in sprints_raw]

            # Issue links -- blockers and clones
            blockers = []
            clones = []
            for link in fields.get('issuelinks', []):
                link_type = link.get('type', {}).get('name', '').lower()
                linked_key = ''
                if 'inwardIssue' in link:
                    linked_key = link['inwardIssue'].get('key', '')
                elif 'outwardIssue' in link:
                    linked_key = link['outwardIssue'].get('key', '')
                if 'block' in link_type:
                    blockers.append(linked_key)
                elif 'clone' in link_type:
                    clones.append(linked_key)

            # Age
            age_days = (TODAY - created_dt).days if created_dt else 0

            # Cycle time (preliminary: total age, refined in compute_metrics)
            cycle_days = None
            if resolved_dt and created_dt:
                cycle_days = (resolved_dt - created_dt).days

            item = {
                'key': issue.get('key', ''),
                'summary': fields.get('summary', ''),
                'type': issue_type,
                'status': status,
                'status_cat': status_cat(status),
                'priority': priority,
                'priority_defined': bool(priority and priority.lower() not in ['undefined', '', 'none', 'trivial']),
                'assignee': assignee,
                'reporter': reporter,
                'created': created_dt,
                'resolved': resolved_dt,
                'points': points_val if points_val is not None else 0,
                'has_estimate': points_val is not None,
                'has_ac': False,  # No separate AC field from API
                'has_ac_in_desc': has_ac_in_desc,
                'age_days': age_days,
                'cycle_days': cycle_days,
                'sprints_raw': sprints_raw,
                'sprint_labels': sprint_labels,
                'sprint_count': len(sprint_labels),
                'blockers': blockers,
                'clones': clones,
                'description': desc_text,
            }
            items.append(item)

        # Pagination
        total = data.get('total', 0)
        start_at += len(issues)
        if start_at >= total or not issues:
            break

    return items, detected_team, sprint_name


# ---------------------------------------------------------------------------
# MCP JSON Parsing (Jira MCP tool output)
# ---------------------------------------------------------------------------

def parse_mcp_json(filepath, target_sprint=None):
    """
    Parse JSON output from Jira MCP tools (e.g., jira_get_sprint_issues or
    jira_search) into the same item dict format as parse_csv/fetch_sprint_issues.

    Handles the nested result wrapper that MCP tools produce, and extracts
    story points from customfield_10028 and sprint history from customfield_10020.

    Args:
        filepath: Path to the JSON file (raw MCP tool output)
        target_sprint: Sprint name or number to filter by (optional)

    Returns:
        tuple: (items_list, team_name, sprint_name)
    """
    with open(filepath, encoding='utf-8') as f:
        raw = json.load(f)

    # Unwrap MCP result envelope: {"result": "{...}"}
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, dict) and 'result' in raw:
        result = raw['result']
        if isinstance(result, str):
            result = json.loads(result)
        raw = result

    issues = raw.get('issues', [])
    if not issues:
        return [], None, None

    detected_team = None
    detected_sprint = None
    items = []

    def _get_custom(issue, field_id):
        """Extract a custom field value from MCP issue data."""
        custom = issue.get('custom_fields', {})
        if custom:
            for key, val in custom.items():
                if field_id in key:
                    if isinstance(val, dict):
                        return val.get('value', val)
                    return val
        val = issue.get(field_id)
        if val is not None:
            if isinstance(val, dict):
                return val.get('value', val)
            return val
        return None

    def _safe_name(obj, fallback=''):
        """Extract display name from a dict or return string."""
        if not obj:
            return fallback
        if isinstance(obj, dict):
            return obj.get('display_name', obj.get('displayName', obj.get('name', fallback)))
        return str(obj)

    for issue in issues:
        # Status
        status_obj = issue.get('status', {})
        status = status_obj.get('name', '') if isinstance(status_obj, dict) else str(status_obj)

        # Issue type
        type_obj = issue.get('issue_type', {})
        issue_type = type_obj.get('name', '') if isinstance(type_obj, dict) else str(type_obj)

        # Priority
        priority_obj = issue.get('priority', {})
        priority = priority_obj.get('name', '') if isinstance(priority_obj, dict) else str(priority_obj)

        # Assignee / Reporter
        assignee = _safe_name(issue.get('assignee'))
        reporter = _safe_name(issue.get('reporter'))

        # Story points -- customfield_10028 (Red Hat Jira), fallback to story_points
        points_val = None
        sp_raw = _get_custom(issue, 'customfield_10028')
        if sp_raw is None:
            sp_raw = issue.get('story_points')
        if sp_raw is not None:
            try:
                points_val = float(sp_raw)
            except (ValueError, TypeError):
                pass

        # Sprint history -- customfield_10020
        sprints_raw = []
        sprint_data = _get_custom(issue, 'customfield_10020')
        if sprint_data and isinstance(sprint_data, list):
            for s in sprint_data:
                if isinstance(s, dict):
                    name = s.get('name', '')
                    if name:
                        sprints_raw.append(name)
                elif isinstance(s, str) and s:
                    sprints_raw.append(s)

        # If no sprint history from custom fields, use target_sprint as fallback
        if not sprints_raw and target_sprint:
            sprints_raw = [target_sprint]

        # Detect team and sprint from sprint names
        if not detected_team and sprints_raw:
            for sr in sprints_raw:
                m = re.match(r'^(.+?)\s*[-–]\s*Sprint\s*(\S+)', sr, re.IGNORECASE)
                if not m:
                    m = re.match(r'^(.+?)\s+Sprint\s*(\S+)', sr, re.IGNORECASE)
                if m:
                    detected_team = m.group(1).strip()
                    break

        sprint_labels = [extract_sprint_number(s) for s in sprints_raw]

        # Filter by target sprint if specified
        if target_sprint:
            target_norm = target_sprint.strip()
            target_num = re.search(r'(\d+\w*)', target_norm)
            target_snum = f"S{target_num.group(1)}" if target_num else target_norm

            in_target = target_snum in sprint_labels
            if not in_target:
                in_target = any(target_norm.lower() in s.lower() for s in sprints_raw)
            if not in_target:
                continue

            if not detected_sprint:
                for sr in sprints_raw:
                    if target_norm.lower() in sr.lower() or target_snum == extract_sprint_number(sr):
                        detected_sprint = sr
                        break
        else:
            if not detected_sprint and sprints_raw:
                detected_sprint = sprints_raw[-1]

        # Epic name -- customfield_10011
        epic_name = _get_custom(issue, 'customfield_10011') or ''

        # Parent
        parent = issue.get('parent', {})
        parent_key = parent.get('key', '') if isinstance(parent, dict) else str(parent or '')

        # Description
        desc_text = issue.get('description', '') or ''
        if isinstance(desc_text, dict):
            desc_text = json.dumps(desc_text)

        # Check for acceptance criteria (scan full text before truncating)
        has_ac_in_desc = has_acceptance_criteria(desc_text)
        desc_text = desc_text[:500]

        # Issue links
        links = issue.get('issuelinks', issue.get('links', []))
        blockers = []
        clones = []
        if links and isinstance(links, list):
            for link in links:
                if not isinstance(link, dict):
                    continue
                link_type = link.get('type', {})
                lt_name = link_type.get('name', '') if isinstance(link_type, dict) else str(link_type)
                for direction in ['inwardIssue', 'inward_issue', 'outwardIssue', 'outward_issue']:
                    linked = link.get(direction, {})
                    if linked:
                        linked_key = linked.get('key', '') if isinstance(linked, dict) else str(linked)
                        if linked_key:
                            if 'block' in lt_name.lower():
                                blockers.append(linked_key)
                            elif 'clone' in lt_name.lower():
                                clones.append(linked_key)

        # Dates
        created_dt = parse_date(issue.get('created', ''))
        resolved_dt = parse_date(issue.get('resolved', ''))
        age_days = (TODAY - created_dt).days if created_dt else 0
        cycle_days = None
        if resolved_dt and created_dt:
            cycle_days = (resolved_dt - created_dt).days

        item = {
            'key': issue.get('key', ''),
            'summary': issue.get('summary', ''),
            'type': issue_type,
            'status': status,
            'status_cat': status_cat(status),
            'priority': priority,
            'priority_defined': bool(priority and priority.lower() not in ['undefined', '', 'none', 'trivial']),
            'assignee': assignee,
            'reporter': reporter,
            'created': created_dt,
            'resolved': resolved_dt,
            'points': points_val if points_val is not None else 0,
            'has_estimate': points_val is not None,
            'has_ac': False,
            'has_ac_in_desc': has_ac_in_desc,
            'age_days': age_days,
            'cycle_days': cycle_days,
            'sprints_raw': sprints_raw,
            'sprint_labels': sprint_labels,
            'sprint_count': len(sprint_labels),
            'blockers': blockers,
            'clones': clones,
            'description': desc_text,
        }
        items.append(item)

    return items, detected_team, detected_sprint


# ---------------------------------------------------------------------------
# Enrichment Data Handling
# ---------------------------------------------------------------------------

def load_enrichment(filepath):
    """Load Jira enrichment data from a JSON file."""
    if not filepath or not os.path.exists(filepath):
        return {}
    with open(filepath) as f:
        return json.load(f)


def generate_enrichment_requests(items):
    """
    Identify items that would benefit most from Jira changelog enrichment.
    Returns a prioritized list of issue keys.
    """
    requests = []

    for item in items:
        priority_score = 0
        reasons = []

        # Multi-sprint items (carryover candidates)
        if item['sprint_count'] >= 2:
            priority_score += 3
            reasons.append(f"In {item['sprint_count']} sprints (carryover pattern)")

        # Old items still open
        if item['age_days'] > 60 and not is_done(item['status']):
            priority_score += 2
            reasons.append(f"{item['age_days']} days old, still open")

        # 0-point items
        if item['points'] == 0 and item['has_estimate']:
            priority_score += 2
            reasons.append("0-point estimate (placeholder?)")

        # Has blockers
        if item['blockers']:
            priority_score += 2
            reasons.append(f"Has blocker links: {', '.join(item['blockers'][:3])}")

        # Has clones
        if item['clones']:
            priority_score += 1
            reasons.append("Has clone links (repurposing risk)")

        # In Progress for a long time
        if item['status_cat'] == 'inprogress' and item['age_days'] > 30:
            priority_score += 1
            reasons.append("In Progress > 30 days")

        # Items in review for a long time
        if item['status_cat'] == 'review' and item['age_days'] > 30:
            priority_score += 1
            reasons.append("In Review > 30 days")

        if priority_score > 0:
            requests.append({
                'key': item['key'],
                'summary': item['summary'][:80],
                'priority_score': priority_score,
                'reasons': reasons,
                'lookup_params': {
                    'issue_key': item['key'],
                    'expand': 'changelog',
                    'comment_limit': 10,
                }
            })

    # Sort by priority score descending
    requests.sort(key=lambda x: x['priority_score'], reverse=True)
    return requests


# ---------------------------------------------------------------------------
# Onboarding / Automation Item Detection
# ---------------------------------------------------------------------------

ONBOARDING_PATTERNS = re.compile(
    r'(ldap|shared.?calendar|introductions?|onboarding|mailing.?list|'
    r'access.?request|new.?hire|welcome|orientation|badge|laptop|setup.?account|'
    r'add.?to.?group|invite.?to|slack.?channel)',
    re.IGNORECASE
)

def is_onboarding_item(item):
    """Detect onboarding/automation items: bot-created Sub-tasks with 0-day cycle times."""
    score = 0
    if item['type'] in ('Sub-task', 'Subtask', 'Sub-Task'):
        score += 1
    if item['cycle_days'] is not None and item['cycle_days'] == 0:
        score += 1
    if ONBOARDING_PATTERNS.search(item.get('summary', '')):
        score += 2
    reporter = (item.get('reporter') or '').lower()
    if 'bot' in reporter or 'automation' in reporter or 'jira' in reporter:
        score += 1
    if item['points'] == 0 and item.get('has_estimate'):
        score += 1
    return score >= 2


# ---------------------------------------------------------------------------
# Metrics Computation
# ---------------------------------------------------------------------------

def compute_metrics(items, enrichment=None):
    """Compute all sprint health metrics from parsed items and optional enrichment."""
    if enrichment is None:
        enrichment = {}

    m = {}
    n = len(items)
    m['total_items'] = n

    if n == 0:
        return m

    # --- Onboarding / Automation detection ---
    onboarding_items = [i for i in items if is_onboarding_item(i)]
    real_items = [i for i in items if not is_onboarding_item(i)]
    m['onboarding_items'] = onboarding_items
    m['onboarding_count'] = len(onboarding_items)
    m['real_item_count'] = len(real_items)
    m['real_points'] = sum(i['points'] for i in real_items)
    m['real_done_points'] = sum(i['points'] for i in real_items if is_done(i['status']))
    m['real_delivery_rate'] = m['real_done_points'] / m['real_points'] if m['real_points'] > 0 else 0

    # --- Points ---
    total_pts = sum(i['points'] for i in items)
    done_items = [i for i in items if is_done(i['status'])]
    done_pts = sum(i['points'] for i in done_items)

    m['total_points'] = total_pts
    m['done_items'] = len(done_items)
    m['done_points'] = done_pts
    m['delivery_rate'] = done_pts / total_pts if total_pts > 0 else 0
    m['item_completion_rate'] = len(done_items) / n if n > 0 else 0

    # --- Status breakdown ---
    status_groups = defaultdict(lambda: {'count': 0, 'points': 0, 'items': []})
    for item in items:
        cat = item['status_cat']
        status_groups[cat]['count'] += 1
        status_groups[cat]['points'] += item['points']
        status_groups[cat]['items'].append(item)

    m['status_groups'] = dict(status_groups)

    # --- Cycle times ---
    # Determine sprint start date: use the earliest created date among items
    # that were created during the sprint window (items created in the last 30 days
    # before the sprint, to avoid old carryover items pulling the date back)
    created_dates = sorted([i['created'] for i in items if i['created']])
    if created_dates:
        # Use the 25th percentile of creation dates as sprint start approximation
        # This avoids old carryover items skewing the start date
        idx = max(0, len(created_dates) // 4)
        sprint_start_approx = created_dates[idx]
    else:
        sprint_start_approx = None

    # For resolved items, compute cycle time as:
    # Resolved - max(Created, Sprint Start)
    # This measures flow time within the sprint context, not total item age
    for item in done_items:
        if item['resolved'] and item['created'] and sprint_start_approx:
            flow_start = max(item['created'], sprint_start_approx)
            item['cycle_days'] = max(0, (item['resolved'] - flow_start).days)

    cycle_times = [(i['key'], i['cycle_days']) for i in done_items if i['cycle_days'] is not None]
    if cycle_times:
        ct_values = [ct for _, ct in cycle_times]
        m['avg_cycle_time'] = round(sum(ct_values) / len(ct_values), 1)
        m['median_cycle_time'] = sorted(ct_values)[len(ct_values) // 2]
        m['min_cycle_time'] = min(ct_values)
        m['max_cycle_time'] = max(ct_values)
        m['cycle_times'] = cycle_times
    else:
        m['avg_cycle_time'] = 0
        m['median_cycle_time'] = 0
        m['min_cycle_time'] = 0
        m['max_cycle_time'] = 0
        m['cycle_times'] = []

    # --- Age distribution ---
    ages = [i['age_days'] for i in items]
    m['avg_age'] = sum(ages) / len(ages) if ages else 0
    m['max_age'] = max(ages) if ages else 0
    m['oldest_item'] = max(items, key=lambda i: i['age_days']) if items else None

    # --- AC coverage ---
    # Acceptance criteria are typically documented in the issue description.
    # Count items that have AC in either a dedicated field or the description.
    ac_field_count = sum(1 for i in items if i['has_ac'])
    ac_desc_count = sum(1 for i in items if i['has_ac_in_desc'])
    ac_any_count = sum(1 for i in items if i['has_ac'] or i['has_ac_in_desc'])
    m['ac_field_count'] = ac_any_count
    m['ac_field_rate'] = ac_any_count / n
    m['ac_desc_count'] = ac_desc_count
    m['ac_any_count'] = ac_any_count

    # --- Priority coverage ---
    priority_defined = sum(1 for i in items if i['priority_defined'])
    m['priority_defined_count'] = priority_defined
    m['priority_defined_rate'] = priority_defined / n
    priority_dist = Counter(i['priority'] for i in items)
    m['priority_distribution'] = dict(priority_dist)

    # --- Issue type distribution ---
    type_dist = Counter(i['type'] for i in items)
    m['type_distribution'] = dict(type_dist)

    # --- Estimate coverage ---
    estimated = sum(1 for i in items if i['has_estimate'])
    zero_pt = sum(1 for i in items if i['has_estimate'] and i['points'] == 0)
    m['estimated_count'] = estimated
    m['unestimated_count'] = n - estimated
    m['zero_point_count'] = zero_pt

    # --- Point distribution ---
    point_dist = Counter(i['points'] for i in items if i['has_estimate'])
    m['point_distribution'] = dict(sorted(point_dist.items()))

    # --- Work distribution ---
    # Normalize assignee names: case-insensitive dedup, canonical = title case
    def _normalize_name(name):
        if not name or name.strip().lower() in ('unassigned', '(unassigned)', ''):
            return '(Unassigned)'
        return name.strip().title()

    assignee_stats = defaultdict(lambda: {
        'items': 0, 'points': 0, 'done_items': 0, 'done_points': 0,
        'issue_keys': [], 'statuses': []
    })
    for item in items:
        a = _normalize_name(item['assignee'])
        assignee_stats[a]['items'] += 1
        assignee_stats[a]['points'] += item['points']
        assignee_stats[a]['issue_keys'].append(item['key'])
        assignee_stats[a]['statuses'].append(item['status'])
        if is_done(item['status']):
            assignee_stats[a]['done_items'] += 1
            assignee_stats[a]['done_points'] += item['points']
    m['assignee_stats'] = dict(assignee_stats)
    # Team size excludes "(Unassigned)"
    m['team_size'] = len([a for a in assignee_stats if a != '(Unassigned)'])

    # --- Sprint carryover ---
    multi_sprint = [i for i in items if i['sprint_count'] >= 2]
    m['multi_sprint_items'] = multi_sprint
    m['max_sprint_carry'] = max((i['sprint_count'] for i in items), default=0)

    # --- Zombie detection ---
    zombies = []
    for item in items:
        is_zombie = False
        reasons = []
        if item['sprint_count'] >= 3:
            is_zombie = True
            reasons.append(f"In {item['sprint_count']}+ sprints")
        if item['age_days'] > 90 and not is_done(item['status']):
            is_zombie = True
            reasons.append(f"{item['age_days']} days old, not done")
        if item['points'] == 0 and item['has_estimate'] and item['sprint_count'] >= 2:
            is_zombie = True
            reasons.append("0 points, multi-sprint")
        if is_zombie:
            zombies.append({**item, 'zombie_reasons': reasons})
    m['zombies'] = zombies

    # --- Blocked items ---
    blocked = [i for i in items if i['blockers']]
    m['blocked_items'] = blocked

    # --- Items never started ---
    never_started = [i for i in items if item['status_cat'] == 'new']
    m['never_started'] = status_groups.get('new', {'count': 0, 'points': 0, 'items': []})

    # --- Overcommitment ratio ---
    if done_pts > 0:
        m['overcommit_ratio'] = total_pts / done_pts
    else:
        m['overcommit_ratio'] = float('inf') if total_pts > 0 else 1

    # --- Health rating ---
    risk_score = 0
    if m['delivery_rate'] < 0.5:
        risk_score += 3
    elif m['delivery_rate'] < 0.7:
        risk_score += 2
    elif m['delivery_rate'] < 0.85:
        risk_score += 1

    if m['ac_field_rate'] < 0.3:
        risk_score += 2
    elif m['ac_field_rate'] < 0.7:
        risk_score += 1

    if len(zombies) >= 3:
        risk_score += 2
    elif len(zombies) >= 1:
        risk_score += 1

    new_pct = status_groups.get('new', {}).get('count', 0) / n if n > 0 else 0
    if new_pct > 0.3:
        risk_score += 2
    elif new_pct > 0.15:
        risk_score += 1

    if m['priority_defined_rate'] < 0.3:
        risk_score += 1

    if risk_score >= 6:
        m['health_rating'] = 'HIGH RISK'
        m['health_css'] = 'high-risk'
    elif risk_score >= 3:
        m['health_rating'] = 'MODERATE RISK'
        m['health_css'] = 'moderate-risk'
    else:
        m['health_rating'] = 'HEALTHY'
        m['health_css'] = 'healthy'
    m['risk_score'] = risk_score

    # --- Top recommendation (single most impactful action) ---
    risk_contributors = []
    if m['delivery_rate'] < 0.5:
        risk_contributors.append((3, 'delivery',
            "Cut sprint scope by 40-50%. The team is completing less than half of committed work -- commit to fewer items and finish them.",
            "Reduce sprint commitment to match actual capacity"))
    elif m['delivery_rate'] < 0.7:
        risk_contributors.append((2, 'delivery',
            "Reduce sprint commitment by 20-30%. The team is consistently over-committing. Use last sprint's velocity as the ceiling, not a target.",
            "Right-size sprint commitment to proven velocity"))
    elif m['delivery_rate'] < 0.85:
        risk_contributors.append((1, 'delivery',
            "Fine-tune sprint planning -- the team is close to predictable delivery but still slightly over-committing.",
            "Tighten sprint commitment to match capacity"))

    if m['ac_field_rate'] < 0.3:
        risk_contributors.append((2, 'ac',
            "Require acceptance criteria on every item before sprint planning. Without AC, 'done' is subjective -- this drives rework and scope creep.",
            "Mandate acceptance criteria as a Definition of Ready gate"))
    elif m['ac_field_rate'] < 0.7:
        risk_contributors.append((1, 'ac',
            "Increase AC coverage -- aim for 80%+ of items having written acceptance criteria before they enter the sprint.",
            "Strengthen Definition of Ready with AC coverage"))

    if len(zombies) >= 3:
        risk_contributors.append((2, 'zombies',
            f"Triage the {len(zombies)} zombie items immediately. Each one should be completed this sprint, descoped, or closed. Zombies that linger create planning debt.",
            f"Resolve or remove {len(zombies)} zombie items"))
    elif len(zombies) >= 1:
        risk_contributors.append((1, 'zombies',
            f"Address {len(zombies)} zombie item(s) -- items carried 3+ sprints need a decision: finish, split, or close.",
            f"Decide on {len(zombies)} zombie item(s)"))

    if new_pct > 0.3:
        risk_contributors.append((2, 'never_started',
            f"{status_groups.get('new', {}).get('count', 0)} items were never started. The team is treating the sprint backlog as a wish list. Only pull in work the team genuinely intends to start.",
            "Stop committing work the team won't start"))
    elif new_pct > 0.15:
        risk_contributors.append((1, 'never_started',
            "Too many items sit untouched. Review capacity at planning and only commit items that have owners and bandwidth.",
            "Align sprint commitment with available capacity"))

    if m['priority_defined_rate'] < 0.3:
        risk_contributors.append((1, 'priority',
            "Most items lack priority. Without prioritization, the team can't make good trade-off decisions when time runs short.",
            "Set priority on all sprint items during planning"))

    # Sort by score descending; pick the top one
    risk_contributors.sort(key=lambda x: x[0], reverse=True)
    if risk_contributors:
        m['top_recommendation'] = risk_contributors[0][2]
        m['top_recommendation_short'] = risk_contributors[0][3]
    else:
        m['top_recommendation'] = "Maintain current practices -- the team is delivering predictably with good process discipline."
        m['top_recommendation_short'] = "Stay the course"

    # --- Enrichment-derived metrics ---
    for item in items:
        edata = enrichment.get(item['key'], {})
        if edata:
            item['enrichment'] = edata
            if 'sprint_history' in edata:
                item['sprint_labels'] = edata['sprint_history']
                item['sprint_count'] = len(edata['sprint_history'])
            if 'changelog_summary' in edata:
                item['changelog_summary'] = edata['changelog_summary']
            if 'comments_summary' in edata:
                item['comments_summary'] = edata['comments_summary']
            if 'has_ac_in_description' in edata:
                item['has_ac_in_desc'] = edata['has_ac_in_description']
            if 'blockers' in edata and edata['blockers']:
                item['blockers'] = edata['blockers']
            if 'repurposed' in edata:
                item['repurposed'] = edata['repurposed']

    # --- Recompute carryover metrics after enrichment updates ---
    m['multi_sprint_items'] = [i for i in items if i['sprint_count'] >= 2]
    m['max_sprint_carry'] = max((i['sprint_count'] for i in items), default=0)

    # Recompute zombies with enrichment-corrected sprint_count
    zombies = []
    for item in items:
        is_zombie = False
        reasons = []
        if item['sprint_count'] >= 3:
            is_zombie = True
            reasons.append(f"In {item['sprint_count']}+ sprints")
        if item['age_days'] > 90 and not is_done(item['status']):
            is_zombie = True
            reasons.append(f"{item['age_days']} days old, not done")
        if item['points'] == 0 and item['has_estimate'] and item['sprint_count'] >= 2:
            is_zombie = True
            reasons.append("0 points, multi-sprint")
        if is_zombie:
            zombies.append({**item, 'zombie_reasons': reasons})
    m['zombies'] = zombies

    return m


# ---------------------------------------------------------------------------
# Anti-Pattern Detection
# ---------------------------------------------------------------------------

def detect_antipatterns(items, metrics, enrichment=None):
    """Detect Agile anti-patterns from the data."""
    patterns = []

    # 1. Chronic Overcommitment
    if metrics['delivery_rate'] < 0.6:
        never_started_count = metrics['status_groups'].get('new', {}).get('count', 0)
        never_started_pts = metrics['status_groups'].get('new', {}).get('points', 0)
        patterns.append({
            'name': 'Chronic Overcommitment',
            'evidence': f"{metrics['total_points']} points committed vs. {metrics['done_points']} delivered ({metrics['delivery_rate']:.0%}). {never_started_count} items ({never_started_pts} pts) never started.",
            'impact': "Erodes trust, makes forecasting impossible. The team never experiences the satisfaction of completing a sprint.",
        })

    # 2. Perpetual Carryover
    zombies = metrics.get('zombies', [])
    if zombies:
        zombie_details = "; ".join(
            f"{z['key']} ({z['sprint_count']} sprints, {z['age_days']}d)"
            for z in zombies[:3]
        )
        patterns.append({
            'name': 'Perpetual Carryover',
            'evidence': f"{len(zombies)} zombie items detected. {zombie_details}.",
            'impact': "Treats the sprint as a rolling backlog. Items lose urgency when they can always move to next sprint.",
        })

    # 3. Item Repurposing
    repurposed = [i for i in items if i.get('repurposed')]
    if repurposed:
        details = "; ".join(f"{i['key']}" for i in repurposed[:3])
        patterns.append({
            'name': 'Item Repurposing',
            'evidence': f"{len(repurposed)} items were repurposed mid-sprint: {details}. Original work history destroyed.",
            'impact': "Violates traceability, makes velocity measurement meaningless, hides scope change.",
        })

    # 4. Hidden Work
    zero_pt = [i for i in items if i['has_estimate'] and i['points'] == 0]
    unestimated = [i for i in items if not i['has_estimate']]
    if zero_pt or unestimated:
        details = f"{len(zero_pt)} items at 0 points" if zero_pt else ""
        if unestimated:
            details += ("; " if details else "") + f"{len(unestimated)} unestimated items"
        patterns.append({
            'name': 'Hidden Work',
            'evidence': f"{details}. These consume attention without capacity allocation.",
            'impact': "Invisible effort consumes capacity. Planning underestimates true workload.",
        })

    # 5. Missing Definition of Ready
    if metrics['ac_field_rate'] < 0.5:
        patterns.append({
            'name': 'Missing Definition of Ready',
            'evidence': f"{metrics['ac_field_count']}/{metrics['total_items']} items have AC. {metrics['priority_defined_count']}/{metrics['total_items']} have defined priority. Items enter sprint without clear scope.",
            'impact': "No objective standard for 'done'. Completion is subjective. Sprint planning is based on summaries, not specifications.",
        })

    # 6. External Dependencies in Sprint
    blocked = metrics.get('blocked_items', [])
    if blocked:
        details = "; ".join(f"{b['key']} blocked by {', '.join(b['blockers'][:2])}" for b in blocked[:3])
        patterns.append({
            'name': 'External Dependencies in Sprint',
            'evidence': f"{len(blocked)} items with blocker links: {details}.",
            'impact': "Creates false WIP, inflates commitment, introduces unpredictable delays.",
        })

    # 7. Zombie Items
    if len(zombies) >= 2:
        details = "; ".join(f"{z['key']} ({', '.join(z['zombie_reasons'])})" for z in zombies[:3])
        patterns.append({
            'name': 'Zombie Items',
            'evidence': f"{len(zombies)} perpetually incomplete items: {details}.",
            'impact': "Creates psychological drag, clutters the board, masks the team's actual WIP.",
        })

    # 8. Scope Instability (many items created recently)
    recent_threshold = 14  # days
    recent_items = [i for i in items if i['age_days'] <= recent_threshold]
    if len(recent_items) > len(items) * 0.2:
        patterns.append({
            'name': 'Scope Instability',
            'evidence': f"{len(recent_items)} of {len(items)} items ({len(recent_items)/len(items):.0%}) created in the last {recent_threshold} days, suggesting ongoing scope addition.",
            'impact': "Late-created items dilute focus on items committed at sprint start. Sprint scope is not protected.",
        })

    return patterns


# ---------------------------------------------------------------------------
# Additional Observations Detection
# ---------------------------------------------------------------------------

def detect_observations(items, metrics, enrichment=None):
    """Detect additional data-driven observations from enrichment data.

    This surfaces noteworthy patterns that don't fit the 8 fixed anti-pattern
    dimensions but are still valuable for sprint health understanding.
    Only observations with actual evidence are returned.
    """
    if enrichment is None:
        enrichment = {}

    observations = []
    m = metrics
    sprint_start = None
    # Try to extract sprint start date from items' sprint data
    for item in items:
        sd = item.get('sprint_start')
        if sd:
            sprint_start = sd
            break

    # 1. Stale carry-overs (items resolved BEFORE sprint started)
    if sprint_start:
        pre_resolved = []
        for item in items:
            if is_done(item['status']) and item.get('resolved_date'):
                try:
                    res_dt = item['resolved_date']
                    if isinstance(res_dt, str):
                        res_dt = res_dt[:10]
                    start_dt = sprint_start[:10] if isinstance(sprint_start, str) else str(sprint_start)[:10]
                    if res_dt < start_dt:
                        pre_resolved.append(item)
                except (TypeError, ValueError):
                    pass
        if pre_resolved:
            observations.append({
                'title': 'Pre-Resolved Items Inflating Sprint',
                'detail': f"{len(pre_resolved)} items were already resolved before the sprint started but remain in the sprint backlog. "
                          f"These inflate the delivery rate without representing actual sprint work.",
                'items': [i['key'] for i in pre_resolved[:5]],
                'severity': 'warning' if len(pre_resolved) > 3 else 'info',
            })

    # 2. Estimation instability (story points changed multiple times)
    est_unstable = []
    for item in items:
        edata = enrichment.get(item['key'], {})
        changelog = edata.get('changelog_summary', '')
        # Count story point change mentions
        sp_changes = len(re.findall(r'(?:Story [Pp]oints?|story_points?)(?:\s+changed|\s*:)', changelog, re.IGNORECASE))
        sp_arrows = len(re.findall(r'\d+\s*->\s*\d+', changelog))
        if sp_changes >= 2 or sp_arrows >= 3:
            est_unstable.append(item['key'])
    if est_unstable:
        observations.append({
            'title': 'Estimation Instability',
            'detail': f"{len(est_unstable)} items had their story points changed multiple times during the sprint, "
                      f"suggesting unclear scope or difficulty in sizing work.",
            'items': est_unstable[:5],
            'severity': 'warning' if len(est_unstable) > 2 else 'info',
        })

    # 3. Sprint bouncing (items added, removed, then re-added)
    bouncers = []
    for item in items:
        edata = enrichment.get(item['key'], {})
        hist = edata.get('sprint_history', [])
        changelog = edata.get('changelog_summary', '')
        # Check for the same sprint appearing multiple times in history
        if hist:
            seen = set()
            for s in hist:
                if s in seen:
                    bouncers.append(item['key'])
                    break
                seen.add(s)
        # Also check changelog for "removed" + "added" patterns for same sprint
        if item['key'] not in bouncers and ('removed' in changelog.lower() and 're-added' in changelog.lower()):
            bouncers.append(item['key'])
    if bouncers:
        observations.append({
            'title': 'Sprint Bouncing',
            'detail': f"{len(bouncers)} items were added to a sprint, removed, and then re-added. "
                      f"This suggests indecision about sprint scope or difficulty in sprint planning.",
            'items': bouncers[:5],
            'severity': 'warning',
        })

    # 4. Priority escalation mid-sprint
    escalated = []
    for item in items:
        edata = enrichment.get(item['key'], {})
        changelog = edata.get('changelog_summary', '')
        if re.search(r'(?:priority|Priority).*(?:Blocker|Critical|escalat)', changelog, re.IGNORECASE):
            escalated.append(item['key'])
    if escalated:
        observations.append({
            'title': 'Mid-Sprint Priority Escalation',
            'detail': f"{len(escalated)} items had their priority escalated during the sprint. "
                      f"Frequent escalation disrupts planned work and indicates external pressure.",
            'items': escalated[:5],
            'severity': 'warning' if len(escalated) > 1 else 'info',
        })

    # 5. Onboarding/sub-task clusters distorting metrics
    subtasks = [i for i in items if i['type'].lower() in ('sub-task', 'subtask')]
    if subtasks and len(subtasks) > len(items) * 0.4:
        zero_pt_subs = [i for i in subtasks if i['points'] == 0]
        observations.append({
            'title': 'Sub-Task Cluster Distorting Metrics',
            'detail': f"{len(subtasks)} of {len(items)} items ({len(subtasks)/len(items):.0%}) are sub-tasks. "
                      f"{len(zero_pt_subs)} have 0 points. Large sub-task clusters inflate item counts "
                      f"and distort delivery rates without representing independent deliverables.",
            'items': [],
            'severity': 'warning',
        })

    # 6. Sprint manager concentration (one person doing most sprint admin)
    managers = {}
    for item in items:
        edata = enrichment.get(item['key'], {})
        actors = edata.get('key_actors', {})
        carried_by = actors.get('carried_by', '')
        if carried_by:
            managers[carried_by] = managers.get(carried_by, 0) + 1
    if managers:
        total_managed = sum(managers.values())
        top_manager = max(managers, key=managers.get)
        top_count = managers[top_manager]
        if top_count > total_managed * 0.5 and total_managed >= 5:
            observations.append({
                'title': 'Sprint Management Concentration',
                'detail': f"{top_manager} is the primary sprint manager for {top_count}/{total_managed} items "
                          f"({top_count/total_managed:.0%}). Heavy concentration creates a single point of failure "
                          f"for sprint administration.",
                'items': [],
                'severity': 'info',
            })

    # 7. Unassigned items in sprint
    unassigned = [i for i in items if i['assignee'] in ('(Unassigned)', '', 'Unassigned', None)]
    if unassigned:
        observations.append({
            'title': 'Unassigned Sprint Items',
            'detail': f"{len(unassigned)} items in the sprint have no assignee. "
                      f"Unowned work is unlikely to be completed and signals planning gaps.",
            'items': [i['key'] for i in unassigned[:5]],
            'severity': 'warning' if len(unassigned) > 2 else 'info',
        })

    # 8. Status regression (items moving backward in workflow)
    regressed = []
    for item in items:
        edata = enrichment.get(item['key'], {})
        changelog = edata.get('changelog_summary', '')
        # Look for backward status moves
        if re.search(r'In Progress\s*(?:->|→|to)\s*(?:New|Backlog|To Do)', changelog, re.IGNORECASE):
            regressed.append(item['key'])
        elif re.search(r'Review\s*(?:->|→|to)\s*(?:In Progress|New)', changelog, re.IGNORECASE):
            regressed.append(item['key'])
    if regressed:
        observations.append({
            'title': 'Status Regression',
            'detail': f"{len(regressed)} items moved backward in the workflow (e.g., In Progress back to New). "
                      f"This suggests blocked work, scope changes, or premature status advancement.",
            'items': regressed[:5],
            'severity': 'warning',
        })

    # 9. Resolve-reopen cycles
    reopened = []
    for item in items:
        edata = enrichment.get(item['key'], {})
        changelog = edata.get('changelog_summary', '')
        if re.search(r'(?:Closed|Done|Resolved)\s*(?:->|→|to)\s*(?:Reopened|In Progress|New|Open)', changelog, re.IGNORECASE):
            reopened.append(item['key'])
    if reopened:
        observations.append({
            'title': 'Resolve-Reopen Cycles',
            'detail': f"{len(reopened)} items were resolved and then reopened. "
                      f"This may indicate incomplete work, missed requirements, or quality issues.",
            'items': reopened[:5],
            'severity': 'warning',
        })

    return observations


# ---------------------------------------------------------------------------
# Report Generation -- Markdown
# ---------------------------------------------------------------------------

def generate_markdown(team_name, sprint_name, sprint_num, items, metrics, antipatterns, enrichment=None, observations=None):
    """Generate the full markdown sprint health report."""
    if enrichment is None:
        enrichment = {}

    m = metrics
    lines = []

    def w(line=""):
        lines.append(line)

    # Extract sprint number for display
    snum = extract_sprint_number(sprint_name) if sprint_name else sprint_num

    # --- Header ---
    w(f"# {snum} Health Report -- {team_name}")
    w()
    w(f"**Report Date:** {REPORT_DATE}")
    w(f"**Sprint:** {sprint_name or snum} (Current Sprint)")
    w(f"**Team:** {team_name}")
    assignees = sorted(a for a in m['assignee_stats'].keys() if a != '(Unassigned)')
    w(f"**Team Members:** {', '.join(assignees)}")
    w()
    w("---")
    w()

    # --- 1. Executive Summary ---
    w("## 1. Executive Summary")
    w()
    w(f"### Sprint Health Rating: {m['health_rating']} (Score: {m['risk_score']})")
    w()
    w(f"> **How this is determined:** The risk score sums points across delivery rate, AC coverage, zombie items, never-started items, and priority coverage. "
      f"**HEALTHY** = 0-2, **MODERATE RISK** = 3-5, **HIGH RISK** = 6+. "
      f"Use as a retro prompt: *\"We scored {m['risk_score']} -- what are the 1-2 biggest contributors we can address next sprint?\"*")
    w()

    # Key findings
    done_g = m['status_groups'].get('done', {'count': 0, 'points': 0})
    new_g = m['status_groups'].get('new', {'count': 0, 'points': 0})
    inprog_g = m['status_groups'].get('inprogress', {'count': 0, 'points': 0})

    w(f"{snum} {'shows significant delivery risk' if m['health_rating'] == 'HIGH RISK' else 'shows moderate delivery risk' if m['health_rating'] == 'MODERATE RISK' else 'is trending healthy'}:")
    w()
    w(f"1. **{m['delivery_rate']:.0%} of committed story points delivered** ({m['done_points']:.0f} of {m['total_points']:.0f} points across {m['done_items']} of {m['total_items']} items)." +
      (f" {new_g['count']} items ({new_g['points']:.0f} pts) were committed but never started." if new_g['count'] > 0 else ""))

    if m['zombies']:
        zombie_strs = [f"{z['key']} ({z['sprint_count']} sprints, {z['age_days']}d)" for z in m['zombies'][:3]]
        w(f"2. **{len(m['zombies'])} zombie items detected** -- " + "; ".join(zombie_strs) + ".")

    if m['ac_field_rate'] < 0.5:
        w(f"{'3' if m['zombies'] else '2'}. **Low acceptance criteria coverage** -- {m['ac_field_count']}/{m['total_items']} items have acceptance criteria.")

    if m['blocked_items']:
        blocked_strs = [f"{b['key']} (blocked by {', '.join(b['blockers'][:2])})" for b in m['blocked_items'][:3]]
        w(f"{'4' if m['zombies'] and m['ac_field_rate'] < 0.5 else '3'}. **External blockers** -- " + "; ".join(blocked_strs) + ".")

    w()

    # Positive signals
    if done_items := [i for i in items if is_done(i['status'])]:
        fast_items = [i for i in done_items if i['cycle_days'] and i['cycle_days'] < 20]
        if fast_items:
            fast_strs = [f"{i['key']} ({i['cycle_days']}d)" for i in fast_items[:3]]
            w(f"**Positive signals:** " + ", ".join(fast_strs) + " showed healthy cycle times." +
              (f" Average cycle time for completed items: {m['avg_cycle_time']:.0f} days (median: {m['median_cycle_time']:.0f})." if m['avg_cycle_time'] > 0 else ""))
            w()

    # Top recommendation
    w(f"> **#1 Recommended Action:** {m['top_recommendation']}")
    w()

    w("---")
    w()

    # --- 2. Key Sprint Observations ---
    w("## 2. Key Sprint Observations")
    w()
    w("| Observation | Detail | Impact |")
    w("|---|---|---|")

    # Delivery rate
    w(f"| {m['done_points']:.0f} of {m['total_points']:.0f} story points delivered | "
      f"{done_g['count']} items resolved. {inprog_g['count']} items ({inprog_g['points']:.0f} pts) still In Progress. "
      f"{new_g['count']} items ({new_g['points']:.0f} pts) never started | "
      f"{'More than two-thirds' if m['delivery_rate'] < 0.34 else 'More than half' if m['delivery_rate'] < 0.5 else 'A significant portion'} of committed work is unfinished |")

    # Zombie items
    for z in m['zombies'][:3]:
        edata = enrichment.get(z['key'], {})
        detail = edata.get('changelog_summary', f"In {z['sprint_count']}+ sprints, {z['age_days']} days old")
        w(f"| Zombie: {z['key']} | {detail} | {'; '.join(z['zombie_reasons'])} |")

    # Blocked items
    for b in m['blocked_items'][:2]:
        edata = enrichment.get(b['key'], {})
        detail = edata.get('changelog_summary', f"Blocked by {', '.join(b['blockers'][:2])}")
        w(f"| Blocked: {b['key']} | {detail} | External dependency creates unpredictable delay |")

    # Good deliveries
    fast = sorted([i for i in items if is_done(i['status']) and i['cycle_days']], key=lambda x: x['cycle_days'])[:3]
    if fast:
        keys = ", ".join(f"{i['key']} ({i['cycle_days']}d)" for i in fast)
        w(f"| Strong deliveries | {keys} -- fastest cycle times | Demonstrates team delivers well on right-sized work |")

    w()
    w("---")
    w()

    # --- 3. Dimension Analysis ---
    w("## 3. Dimension Analysis")
    w()

    # 3.1 Sprint Commitment Reliability
    w("### 3.1 Sprint Commitment Reliability")
    w()
    w("**Observations:**")
    w(f"- Total commitment: {m['total_points']:.0f} story points across {m['total_items']} items.")
    w(f"- Delivered: {m['done_points']:.0f} points ({m['delivery_rate']:.0%}) across {m['done_items']} items.")
    remaining = m['total_points'] - m['done_points']
    w(f"- Remaining: {remaining:.0f} points still open -- " +
      ", ".join(f"{v['points']:.0f} {k.title()}" for k, v in m['status_groups'].items() if k != 'done' and v['count'] > 0) + ".")
    unresolved_carryover = [i for i in m['multi_sprint_items'] if not is_done(i['status'])]
    if unresolved_carryover:
        w(f"- {len(unresolved_carryover)} unresolved items appear in multiple sprints (carryover candidates).")
    elif m['multi_sprint_items']:
        w(f"- {len(m['multi_sprint_items'])} items appear in multiple sprints, but all are now resolved.")
    w(f"- {new_g['count']} items ({new_g['points']:.0f} points) remain in 'New' status -- committed but never started.")
    w()
    w("**Potential Risks:**")
    if m['overcommit_ratio'] > 2:
        w(f"- The sprint was committed at {m['total_points']:.0f} points, but throughput is {m['done_points']:.0f} points. Commitment is ~{m['overcommit_ratio']:.0f}x actual capacity.")
    if m['delivery_rate'] < 0.7:
        unfinished_pts = m['total_points'] - m['done_points']
        w(f"- The next sprint will inherit {unfinished_pts:.0f} points of unfinished work if the team follows a carry-forward pattern.")
    elif m['delivery_rate'] >= 0.85:
        w("- Low carryover risk -- the team completed the vast majority of committed work.")
    w()
    w("**Coaching Recommendations:**")
    target_capacity = max(m['done_points'] * 1.1, 10)
    w(f"- Establish a sprint capacity target based on historical velocity: ~{target_capacity:.0f} points.")
    w("- Use zero-based loading: start empty, pull only what the team can realistically finish.")
    w("- Target 85%+ completion rate within 3 sprints.")
    w()
    w("---")
    w()

    # 3.2 Scope Stability
    w("### 3.2 Scope Stability")
    w()
    w("**Observations:**")
    recent = [i for i in items if i['age_days'] <= 14]
    w(f"- {len(recent)} items were created within the last 14 days of the sprint, suggesting {'significant ' if len(recent) > len(items) * 0.2 else ''}ongoing scope addition.")
    repurposed = [i for i in items if i.get('repurposed')]
    if repurposed:
        for r in repurposed:
            w(f"- **{r['key']} was repurposed mid-sprint:** {r.get('enrichment', {}).get('changelog_summary', 'Details available with Jira enrichment')}")
    zero_pt_items = [i for i in items if i['has_estimate'] and i['points'] == 0]
    if zero_pt_items:
        keys = ", ".join(i['key'] for i in zero_pt_items)
        w(f"- {len(zero_pt_items)} items at 0 points ({keys}) -- process items not reflected in capacity planning.")
    w()
    w("**Coaching Recommendations:**")
    w("- Never repurpose existing items. Create new Jira issues for new work.")
    w("- Establish a sprint scope freeze after planning. New items require a trade-off: equal-sized work must be removed.")
    w("- Tag mid-sprint additions as 'unplanned' to make scope injection visible.")
    w()
    w("---")
    w()

    # 3.3 Flow Efficiency
    w("### 3.3 Flow Efficiency")
    w()
    w("**Observations:**")
    if new_g['count'] > 0:
        w(f"- **{new_g['count']} items ({new_g['points']:.0f} pts) in 'New' status** -- added to the sprint but never started.")

    testing_g = m['status_groups'].get('testing', {'count': 0, 'points': 0, 'items': []})
    if testing_g['count'] > 0:
        w(f"- **{testing_g['count']} item(s) in Testing** ({testing_g['points']:.0f} pts) -- may indicate a testing bottleneck.")

    review_g = m['status_groups'].get('review', {'count': 0, 'points': 0, 'items': []})
    if review_g['count'] > 0:
        old_reviews = [i for i in review_g['items'] if i['age_days'] > 30]
        w(f"- **{review_g['count']} item(s) in Review** ({review_g['points']:.0f} pts)." +
          (f" {len(old_reviews)} have been in review for 30+ days." if old_reviews else ""))

    if m['blocked_items']:
        for b in m['blocked_items'][:2]:
            w(f"- **{b['key']} blocked** by {', '.join(b['blockers'][:2])}.")

    if m['cycle_times']:
        w("- Cycle times for completed items:")
        fast_ct = [(k, ct) for k, ct in m['cycle_times'] if ct < 14]
        med_ct = [(k, ct) for k, ct in m['cycle_times'] if 14 <= ct < 30]
        slow_ct = [(k, ct) for k, ct in m['cycle_times'] if 30 <= ct < 60]
        vslow_ct = [(k, ct) for k, ct in m['cycle_times'] if ct >= 60]
        if fast_ct:
            w(f"  - Fast (<2 weeks): " + ", ".join(f"{k} ({ct}d)" for k, ct in fast_ct))
        if med_ct:
            w(f"  - Medium (2-4 weeks): " + ", ".join(f"{k} ({ct}d)" for k, ct in med_ct))
        if slow_ct:
            w(f"  - Slow (1-2 months): " + ", ".join(f"{k} ({ct}d)" for k, ct in slow_ct))
        if vslow_ct:
            w(f"  - Very slow (2+ months): " + ", ".join(f"{k} ({ct}d)" for k, ct in vslow_ct))
    w()
    w("**Coaching Recommendations:**")
    w("- Only pull items into the sprint when they will be started within 3 days.")
    w("- Implement WIP limits: 2-3 active items per engineer.")
    w("- Establish a review SLA: items in Review for more than 3 business days should be escalated.")
    w()
    w("---")
    w()

    # 3.4 Story Size and Work Decomposition
    w("### 3.4 Story Size and Work Decomposition")
    w()
    w("**Observations:**")
    pd = m['point_distribution']
    if pd:
        dist_str = ", ".join(f"{pts:.0f}-pt ({cnt})" for pts, cnt in sorted(pd.items()))
        w(f"- Story point distribution: {dist_str}.")
    if m['unestimated_count'] > 0:
        w(f"- {m['unestimated_count']} items have no estimate.")
    if m['zero_point_count'] > 0:
        w(f"- {m['zero_point_count']} items at 0 points represent unestimated or placeholder work.")
    w()
    w("**Coaching Recommendations:**")
    w("- Every item in the sprint must have a non-zero estimate.")
    w("- Items over 5 points should be broken down before entering a sprint.")
    w()
    w("---")
    w()

    # 3.5 Work Distribution
    w("### 3.5 Work Distribution")
    w()
    w("**Observations:**")
    sorted_assignees = sorted(m['assignee_stats'].items(), key=lambda x: x[1]['items'], reverse=True)
    for assignee, stats in sorted_assignees:
        done_str = f"{stats['done_items']} completed ({stats['done_points']:.0f} pts)" if stats['done_items'] > 0 else "0 completed"
        w(f"- **{assignee}: {stats['items']} items, {stats['points']:.0f} pts** -- {done_str}.")
    w()
    w("**Coaching Recommendations:**")
    max_items = sorted_assignees[0][1]['items'] if sorted_assignees else 0
    if max_items > 5:
        w(f"- Redistribute work. {sorted_assignees[0][0]} has {max_items} items -- cap at 3-4 per person.")
    w("- Investigate team members with 0 completions -- are they blocked or working on untracked items?")
    w(f"- For the next sprint, cap individual commitment at 3-4 items per person.")
    w()
    w("---")
    w()

    # 3.6 Blocker Analysis
    w("### 3.6 Blocker Analysis")
    w()
    w("**Observations:**")
    if m['blocked_items']:
        for b in m['blocked_items']:
            edata = enrichment.get(b['key'], {})
            detail = edata.get('changelog_summary', f"Blocked by {', '.join(b['blockers'])}")
            w(f"- **{b['key']}** -- {b['summary'][:60]}. {detail}.")
    else:
        w("- No formal blocker links detected in the CSV data. Jira enrichment may reveal additional blockers from changelogs and comments.")

    if m['zombies']:
        w()
        for z in m['zombies']:
            edata = enrichment.get(z['key'], {})
            detail = edata.get('changelog_summary', "; ".join(z['zombie_reasons']))
            w(f"- **{z['key']}** (zombie) -- {z['summary'][:60]}. {detail}.")
    w()
    w("**Coaching Recommendations:**")
    w("- Track external dependencies on a separate board. Items blocked by other teams should not count against sprint capacity.")
    w("- Establish a maximum sprint lifespan: any item in 2 consecutive sprints must be re-evaluated.")
    w()
    w("---")
    w()

    # 3.7 Backlog Health
    w("### 3.7 Backlog Health")
    w()
    w("**Observations:**")
    w(f"- **AC coverage:** {m['ac_field_count']}/{m['total_items']} items have acceptance criteria.")
    w(f"- **Priority coverage:** {m['priority_defined_count']}/{m['total_items']} items have defined priority ({m['priority_defined_rate']:.0%}).")
    if m['priority_distribution']:
        w(f"  - Distribution: " + ", ".join(f"{k}: {v}" for k, v in sorted(m['priority_distribution'].items(), key=lambda x: -x[1])))
    w(f"- **Issue types:** " + ", ".join(f"{k} ({v})" for k, v in sorted(m['type_distribution'].items(), key=lambda x: -x[1])))
    w()
    w("**Coaching Recommendations:**")
    w("- Establish a 'no AC, no sprint' gate: items without testable acceptance criteria cannot enter the sprint.")
    w("- Set priority on all items at minimum (Critical/Major/Normal).")
    w("- Use issue types deliberately: Story = feature, Bug = defect, Task = technical, Spike = investigation.")
    w()
    w("---")
    w()

    # 3.8 Delivery Predictability
    w("### 3.8 Delivery Predictability")
    w()
    w("**Observations:**")
    w(f"- Completed points ({m['done_points']:.0f}) vs. committed ({m['total_points']:.0f}) = {m['delivery_rate']:.0%} delivery rate.")
    if m['cycle_times']:
        w(f"- Average cycle time: {m['avg_cycle_time']:.0f} days. Median: {m['median_cycle_time']:.0f} days. Range: {m['min_cycle_time']}-{m['max_cycle_time']} days.")
    if m['multi_sprint_items']:
        w(f"- {len(m['multi_sprint_items'])} items appear in multiple sprints, indicating a chronic carryover pattern.")
    w()
    w("**Coaching Recommendations:**")
    w("- Adopt a 'fresh sprint' policy: incomplete items return to backlog at sprint boundary.")
    w(f"- Target commitment of ~{target_capacity:.0f} points (based on current {m['done_points']:.0f}-point throughput).")
    w("- Use cycle time > 2 sprints as a health flag for investigation.")
    w()
    w("---")
    w()

    # --- 4. Anti-Patterns ---
    w("## 4. Agile Anti-Patterns Detected")
    w()
    if antipatterns:
        w("| Anti-Pattern | Evidence | Why It's Problematic |")
        w("|---|---|---|")
        for ap in antipatterns:
            w(f"| **{ap['name']}** | {ap['evidence']} | {ap['impact']} |")
    else:
        w("No significant anti-patterns detected. This is a positive signal.")
    w()
    w("---")
    w()

    # --- 5. Flow Improvement Opportunities ---
    w("## 5. Flow Improvement Opportunities")
    w()
    w("### Cycle Time Reduction")
    if new_g['count'] > 0:
        w(f"- **Remove sprint queue time.** {new_g['count']} items in 'New' were never started. Only add items when the team has capacity to start within 3 days.")
    w(f"- **Implement WIP limits** (2-3 active items per engineer).")
    long_running = [i for i in items if not is_done(i['status']) and i['age_days'] > 45]
    if long_running:
        keys = ", ".join(f"{i['key']} ({i['age_days']}d)" for i in long_running[:3])
        w(f"- **Address long-running items.** {keys} need focused completion or re-scoping.")
    w()
    w("### Throughput Improvement")
    near_done = [i for i in items if i['status_cat'] in ('review', 'testing')]
    if near_done:
        keys = ", ".join(f"{i['key']} ({i['points']:.0f}pts, {i['status']})" for i in near_done[:4])
        w(f"- **Focus on items closest to Done.** {keys} -- prioritize reviewer/tester time.")
    if m['zombies']:
        w(f"- **Remove zombie items.** " + ", ".join(f"{z['key']}" for z in m['zombies'][:3]) + " should exit the sprint immediately.")
    w()
    w("### Delivery Predictability")
    w(f"- **Right-size commitment** to ~{target_capacity:.0f} points with a 10% unplanned buffer.")
    w("- **Track external dependencies separately.** Items blocked by other teams on a dependency board.")
    w("- **Track flow metrics:** completion ratio, cycle time, WIP count, carryover rate.")
    w()
    w("---")
    w()

    # --- 6. Backlog Improvement Opportunities ---
    w("## 6. Backlog Improvement Opportunities")
    w()
    w("### Structural Issues Identified")
    w()
    issue_num = 1
    if m['ac_field_rate'] < 0.5:
        w(f"{issue_num}. **Low acceptance criteria coverage** -- {m['ac_field_count']}/{m['total_items']} items have acceptance criteria.")
        issue_num += 1
    if m['priority_defined_rate'] < 0.5:
        w(f"{issue_num}. **Priority field underused** -- {100 - m['priority_defined_rate']*100:.0f}% of items have 'Undefined' priority.")
        issue_num += 1
    if len(m['type_distribution']) <= 2:
        dom_type = max(m['type_distribution'], key=m['type_distribution'].get)
        w(f"{issue_num}. **Issue type uniformity** -- {m['type_distribution'][dom_type]} of {m['total_items']} items are {dom_type}s.")
        issue_num += 1
    if m['zero_point_count'] > 0:
        w(f"{issue_num}. **Unestimated work** -- {m['zero_point_count']} items at 0 points.")
    w()
    w("### Recommendations for Backlog Refinement")
    w()
    w("- **Adopt a standard description template.** Overview, Technical Details, Acceptance Criteria (checklist), Dependencies.")
    w("- **Establish a 'no AC, no sprint' gate.** Every item must have testable acceptance criteria.")
    w("- **Use issue types deliberately.** Story = feature, Bug = defect, Task = technical, Spike = investigation.")
    w("- **Conduct a backlog cleanup session (60 min):** remove items older than 3 sprints, add AC to top 15 items, estimate all items, set priorities.")
    w("- **Two-sprint expiry rule.** No automatic carryover -- re-scope, re-assign, or remove.")
    w()
    w("---")
    w()

    # --- 7. Top 5 Actions ---
    w("## 7. Top 5 Actions for the Next Sprint")
    w()
    w("| # | Action | Expected Impact | Evidence |")
    w("|---|---|---|---|")

    actions = generate_top_actions(items, metrics, antipatterns)
    for i, action in enumerate(actions[:5], 1):
        w(f"| {i} | **{action['title']}** | {action['impact']} | {action['evidence']} |")
    w()
    w("---")
    w()

    # --- 8. Coaching Notes ---
    w("## 8. Agile Coaching Notes")
    w()
    w("### For the Sprint Retrospective")
    w()
    w("**Suggested focus areas:**")
    w(f'- "We committed {m["total_points"]:.0f} points and delivered {m["done_points"]:.0f}. What would a sprint look like where we complete 85% of what we commit?"')
    if m['zombies']:
        z = m['zombies'][0]
        w(f'- "{z["key"]} has been in our sprint for {z["sprint_count"]}+ sprints. What would it take to either define it concretely or decide it\'s not a priority?"')
    w()
    w("**Facilitation tips:**")
    w(f"- Frame the {m['delivery_rate']:.0%} completion rate as a **planning** problem, not a **performance** problem. The team delivered {m['done_points']:.0f} points of actual work.")
    w("- Lead with the positive: highlight completed items and good delivery patterns.")
    w()
    w("### For Sprint Planning")
    w()
    w("**Key principles:**")
    w(f"- **Zero-based loading:** Start with an empty sprint. Pull items deliberately.")
    w(f"- **Capacity calculation:** ~{target_capacity:.0f} points. Subtract 3-5 points for unplanned work buffer.")
    w("- **Readiness gate:** Every item must have AC, estimate (>0), owner, and priority.")
    w("- **Dependency check:** Items blocked by external teams enter only when unblocked.")
    w()

    # --- 9. Additional Observations ---
    if observations:
        w("---")
        w()
        w("## 9. Additional Observations")
        w()
        w("The following patterns were detected from enrichment data (changelogs, comments, sprint history) and may warrant discussion.")
        w()
        for obs in observations:
            severity_icon = {"warning": "**[!]**", "info": "[i]"}.get(obs['severity'], "")
            w(f"### {severity_icon} {obs['title']}")
            w()
            w(obs['detail'])
            if obs.get('items'):
                w()
                w("Affected items: " + ", ".join(obs['items']))
            w()

    # --- Appendix ---
    w("---")
    w()
    w("## Appendix: Sprint Item Tracker")
    w()
    w("| Issue Key | Type | Status | Points | Assignee | Age (days) | Sprint History | Notes |")
    w("|---|---|---|---|---|---|---|---|")

    sorted_items = sorted(items, key=lambda i: i['age_days'])
    for item in sorted_items:
        sprint_hist = "->".join(item['sprint_labels']) if item['sprint_labels'] else "-"
        # Build notes
        notes_parts = []
        if is_done(item['status']) and item['cycle_days']:
            notes_parts.append(f"completed ({item['cycle_days']}d cycle)")
        if item.get('enrichment', {}).get('changelog_summary'):
            notes_parts.append(item['enrichment']['changelog_summary'][:80])
        elif item['summary']:
            notes_parts.append(item['summary'][:60])
        if any('zombie' in str(r).lower() or item['sprint_count'] >= 3 for r in item.get('zombie_reasons', [])):
            notes_parts.insert(0, "ZOMBIE")
        notes = "; ".join(notes_parts) if notes_parts else item['summary'][:60]

        w(f"| {item['key']} | {item['type']} | {item['status']} | {item['points']:.0f} | {item['assignee']} | {item['age_days']} | {sprint_hist} | {notes} |")

    w()
    w("---")
    w()
    w("*This report is intended to support the team's continuous improvement journey. The observations and recommendations are systemic in nature and should be discussed collaboratively.*")

    return "\n".join(lines)


def generate_top_actions(items, metrics, antipatterns):
    """Generate prioritized list of recommended actions."""
    actions = []
    m = metrics

    # 1. Right-size commitment (if overcommitted)
    if m['delivery_rate'] < 0.7:
        target = max(m['done_points'] * 1.1, 10)
        actions.append({
            'title': 'Right-size the sprint commitment',
            'impact': f"Target ~{target:.0f} story points. Use zero-based loading. Enable the team to experience completing a sprint.",
            'evidence': f"{m['total_points']:.0f} pts committed vs. {m['done_points']:.0f} delivered ({m['delivery_rate']:.0%}). {m['status_groups'].get('new', {}).get('count', 0)} items never started.",
            'priority': 10,
        })

    # 2. Remove zombies
    if m['zombies']:
        zombie_strs = ", ".join(f"{z['key']}" for z in m['zombies'][:3])
        actions.append({
            'title': 'Remove zombie items',
            'impact': f"Move {zombie_strs} out of the sprint. Reduces noise, creates focus, reclaims board clarity.",
            'evidence': f"{len(m['zombies'])} items with {m['zombies'][0]['sprint_count']}+ sprints or 90+ days without completion.",
            'priority': 9,
        })

    # 3. AC standard
    if m['ac_field_rate'] < 0.5:
        actions.append({
            'title': 'Establish acceptance criteria standard',
            'impact': "Every item must have testable AC before entering the sprint. Provides objective 'done' criteria.",
            'evidence': f"{m['ac_field_count']}/{m['total_items']} items have AC.",
            'priority': 8,
        })

    # 4. External dependencies
    if m['blocked_items']:
        actions.append({
            'title': 'Track external dependencies separately',
            'impact': "Move blocked items to a dependency board. They enter sprint only when unblocked.",
            'evidence': f"{len(m['blocked_items'])} items blocked by external teams.",
            'priority': 7,
        })

    # 5. WIP limits
    if m['status_groups'].get('new', {}).get('count', 0) > m['total_items'] * 0.2:
        actions.append({
            'title': 'Implement WIP limits and reduce sprint loading',
            'impact': f"Only pull items when the team has capacity. {m['status_groups']['new']['count']} items never started.",
            'evidence': f"{m['status_groups']['new']['count']} items in 'New' status represent wasted commitment.",
            'priority': 6,
        })

    # 6. Priority coverage
    if m['priority_defined_rate'] < 0.3:
        actions.append({
            'title': 'Set priority on all sprint items',
            'impact': "Enables data-driven trade-off decisions at sprint end.",
            'evidence': f"{m['priority_defined_count']}/{m['total_items']} items have defined priority.",
            'priority': 5,
        })

    # 7. Estimate coverage
    if m['zero_point_count'] > 0 or m['unestimated_count'] > 0:
        actions.append({
            'title': 'Estimate all sprint items',
            'impact': "No 0-point or unestimated items in the sprint. Makes capacity planning accurate.",
            'evidence': f"{m['zero_point_count']} items at 0 pts, {m['unestimated_count']} unestimated.",
            'priority': 4,
        })

    # Sort by priority
    actions.sort(key=lambda x: x['priority'], reverse=True)
    return actions[:5]


# ---------------------------------------------------------------------------
# Sprint History Tracking & Trend Charts
# ---------------------------------------------------------------------------

def load_history(filepath):
    """Load sprint history from a JSON file."""
    if filepath and os.path.exists(filepath):
        with open(filepath, 'r') as f:
            return json.load(f)
    return []


def save_history(filepath, history, sprint_name, metrics):
    """Append current sprint metrics to history and save."""
    m = metrics
    entry = {
        'sprint': sprint_name,
        'date': REPORT_DATE,
        'total_points': m['total_points'],
        'done_points': m['done_points'],
        'delivery_rate': round(m['delivery_rate'], 3),
        'total_items': m['total_items'],
        'done_items': m['done_items'],
        'item_completion_rate': round(m['item_completion_rate'], 3),
        'carryover_items': len(m['multi_sprint_items']),
        'zombie_count': len(m['zombies']),
        'avg_cycle_time': round(m['avg_cycle_time'], 1),
        'team_size': m['team_size'],
        'ac_coverage': round(m['ac_field_rate'], 3),
        'risk_score': m['risk_score'],
        'health_rating': m['health_rating'],
        'antipattern_count': 0,  # filled by caller
    }

    # Replace existing entry for same sprint, or append
    existing = [h for h in history if h['sprint'] != sprint_name]
    existing.append(entry)
    # Sort by sprint name
    existing.sort(key=lambda x: x['sprint'])

    with open(filepath, 'w') as f:
        json.dump(existing, f, indent=2)
    return existing


def generate_trend_svg(history, metric_key, label, color='#2563eb', height=120, width=400):
    """Generate an inline SVG sparkline/trend chart for a given metric across sprints."""
    if len(history) < 2:
        return ''

    values = [h.get(metric_key, 0) for h in history[-6:]]  # Last 6 sprints
    labels = [h.get('sprint', '?') for h in history[-6:]]
    n = len(values)
    max_val = max(values) if values else 1
    min_val = min(values) if values else 0
    val_range = max_val - min_val if max_val != min_val else 1

    padding_x = 40
    padding_y = 20
    chart_w = width - padding_x * 2
    chart_h = height - padding_y * 2

    points = []
    for i, v in enumerate(values):
        x = padding_x + (i / (n - 1)) * chart_w if n > 1 else padding_x + chart_w / 2
        y = padding_y + chart_h - ((v - min_val) / val_range) * chart_h
        points.append((x, y, v))

    polyline = ' '.join(f'{x:.0f},{y:.0f}' for x, y, _ in points)

    # Build SVG
    svg = f'<svg width="{width}" height="{height + 30}" viewBox="0 0 {width} {height + 30}" xmlns="http://www.w3.org/2000/svg" style="font-family: var(--font); overflow: visible;">\n'
    svg += f'  <text x="{width/2}" y="14" text-anchor="middle" font-size="12" font-weight="600" fill="var(--text-secondary)">{label}</text>\n'
    svg += f'  <g transform="translate(0, 10)">\n'

    # Grid lines
    for i in range(3):
        gy = padding_y + i * chart_h / 2
        gv = max_val - i * val_range / 2
        fmt_v = f'{gv:.0%}' if max_val <= 1 else f'{gv:.0f}'
        svg += f'    <line x1="{padding_x}" y1="{gy}" x2="{width - padding_x}" y2="{gy}" stroke="var(--border)" stroke-dasharray="3"/>\n'
        svg += f'    <text x="{padding_x - 5}" y="{gy + 4}" text-anchor="end" font-size="10" fill="var(--text-muted)">{fmt_v}</text>\n'

    # Line
    svg += f'    <polyline points="{polyline}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round"/>\n'

    # Dots and labels
    for i, (x, y, v) in enumerate(points):
        fmt_v = f'{v:.0%}' if max_val <= 1 else f'{v:.0f}'
        svg += f'    <circle cx="{x:.0f}" cy="{y:.0f}" r="4" fill="{color}" stroke="#fff" stroke-width="1.5"/>\n'
        svg += f'    <text x="{x:.0f}" y="{y - 8:.0f}" text-anchor="middle" font-size="10" font-weight="600" fill="{color}">{fmt_v}</text>\n'
        # Sprint label at bottom
        sprint_label = extract_sprint_number(labels[i]) or labels[i]
        svg += f'    <text x="{x:.0f}" y="{height - 2}" text-anchor="middle" font-size="9" fill="var(--text-muted)">{sprint_label}</text>\n'

    svg += '  </g>\n</svg>\n'
    return svg


def _trend_explanation(history):
    """Generate interpretive commentary for the trend charts."""
    recent = history[-6:]
    n = len(recent)
    lines = []

    # Identify teams involved
    team_names = []
    for h in recent:
        sname = h.get('sprint', '')
        # Strip sprint number to get team prefix
        team = re.sub(r'\s*Sprint\s*\d+\w*\s*$', '', sname, flags=re.IGNORECASE).strip()
        if team and team not in team_names:
            team_names.append(team)

    if len(team_names) > 1:
        lines.append(
            f'<strong>Note:</strong> This history includes sprints from {len(team_names)} different teams '
            f'({", ".join(team_names)}). Cross-team comparisons should be interpreted with caution — '
            f'differences in velocity or cycle time may reflect team size, scope complexity, or estimation '
            f'conventions rather than performance gaps. For the most meaningful trends, accumulate history '
            f'from the same team across multiple sprints using the <code>--history</code> flag.'
        )

    # Velocity direction
    velocities = [h.get('done_points', 0) for h in recent]
    if len(velocities) >= 2:
        first, last = velocities[0], velocities[-1]
        if last > first * 1.15:
            lines.append(f'<strong>Velocity</strong> increased from {first:.0f} to {last:.0f} points — a positive trajectory if scope quality is maintained.')
        elif last < first * 0.85:
            lines.append(f'<strong>Velocity</strong> declined from {first:.0f} to {last:.0f} points. Investigate whether this reflects capacity changes, scope inflation, or delivery bottlenecks.')
        else:
            lines.append(f'<strong>Velocity</strong> is relatively stable ({first:.0f} → {last:.0f} points).')

    # Delivery rate direction
    rates = [h.get('delivery_rate', 0) for h in recent]
    if len(rates) >= 2:
        first, last = rates[0], rates[-1]
        if last > first + 0.1:
            lines.append(f'<strong>Delivery rate</strong> improved from {first:.0%} to {last:.0%} — the team is completing a larger share of committed work.')
        elif last < first - 0.1:
            lines.append(f'<strong>Delivery rate</strong> dropped from {first:.0%} to {last:.0%}. This suggests over-commitment or mid-sprint disruptions are increasing.')
        else:
            lines.append(f'<strong>Delivery rate</strong> is holding steady around {last:.0%}. Target is 70%+ for healthy sprints.')

    # Carryover direction
    carries = [h.get('carryover_items', 0) for h in recent]
    if len(carries) >= 2:
        first, last = carries[0], carries[-1]
        if last > first:
            lines.append(f'<strong>Carryover</strong> increased from {first} to {last} items — incomplete work is accumulating. Review sprint commitment sizing and blockers.')
        elif last < first:
            lines.append(f'<strong>Carryover</strong> decreased from {first} to {last} items — the team is finishing more of what it starts.')
        else:
            lines.append(f'<strong>Carryover</strong> is flat at {last} items.')

    # Cycle time direction
    cycles = [h.get('avg_cycle_time', 0) for h in recent]
    if len(cycles) >= 2:
        first, last = cycles[0], cycles[-1]
        if last > first * 1.3:
            lines.append(f'<strong>Avg cycle time</strong> grew from {first:.0f} to {last:.0f} days — items are taking longer to complete. Look for WIP overload or blocked queues.')
        elif last < first * 0.7:
            lines.append(f'<strong>Avg cycle time</strong> shortened from {first:.0f} to {last:.0f} days — work is flowing faster through the system.')
        else:
            lines.append(f'<strong>Avg cycle time</strong> is around {last:.0f} days (was {first:.0f}).')

    if not lines:
        return ''

    items_html = ''.join(f'<li style="margin-bottom: 6px;">{l}</li>' for l in lines)
    return f'''
  <div style="background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 16px 20px; margin-top: 16px; font-size: 13px; line-height: 1.6;">
    <h4 style="margin: 0 0 10px 0; font-size: 14px; color: var(--text-primary);">How to Read These Trends</h4>
    <ul style="margin: 0; padding-left: 20px; color: var(--text-secondary);">
      {items_html}
    </ul>
  </div>
'''


def generate_trend_section(history):
    """Generate the full trend charts HTML section."""
    if len(history) < 2:
        return ''

    html = '''
<!-- Trend Charts -->
<div class="section" id="section-trends">
  <div class="section-number">Trends</div>
  <h2>Sprint-over-Sprint Trends</h2>
  <p style="font-size:13px; color: var(--text-secondary); margin-bottom: 16px;">Tracking key metrics across the last {n} sprints to identify improvement trajectories.</p>
  <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); gap: 20px; margin: 16px 0;">
    <div style="border: 1px solid var(--border); border-radius: 8px; padding: 16px; text-align: center;">
      {velocity_chart}
    </div>
    <div style="border: 1px solid var(--border); border-radius: 8px; padding: 16px; text-align: center;">
      {completion_chart}
    </div>
    <div style="border: 1px solid var(--border); border-radius: 8px; padding: 16px; text-align: center;">
      {carryover_chart}
    </div>
    <div style="border: 1px solid var(--border); border-radius: 8px; padding: 16px; text-align: center;">
      {cycle_chart}
    </div>
  </div>
  {explanation}
</div>

'''
    html = html.replace('{n}', str(len(history[-6:])))
    html = html.replace('{velocity_chart}', generate_trend_svg(history, 'done_points', 'Velocity (Story Points Delivered)', '#16a34a'))
    html = html.replace('{completion_chart}', generate_trend_svg(history, 'delivery_rate', 'Delivery Rate', '#2563eb'))
    html = html.replace('{carryover_chart}', generate_trend_svg(history, 'carryover_items', 'Carryover Items', '#dc2626'))
    html = html.replace('{cycle_chart}', generate_trend_svg(history, 'avg_cycle_time', 'Avg Cycle Time (days)', '#f59e0b'))
    html = html.replace('{explanation}', _trend_explanation(history))
    return html


# ---------------------------------------------------------------------------
# Report Generation -- HTML
# ---------------------------------------------------------------------------

def generate_html(team_name, sprint_name, sprint_num, items, metrics, antipatterns, enrichment=None, jira_base_url=None, history=None, observations=None):
    """Generate the full styled HTML sprint health report."""
    if enrichment is None:
        enrichment = {}
    if history is None:
        history = []

    m = metrics
    snum = extract_sprint_number(sprint_name) if sprint_name else sprint_num

    def _issue_link(key):
        """Wrap an issue key in a Jira link if base URL is available."""
        if jira_base_url:
            url = f'{jira_base_url.rstrip("/")}/browse/{key}'
            return f'<a href="{url}" target="_blank" class="issue-key">{key}</a>'
        return f'<span class="issue-key">{key}</span>'

    # Compute next sprint label for action headers
    sprint_match = re.search(r'(\d+)', snum or '')
    if sprint_match:
        _next_sprint_label = f'Sprint {int(sprint_match.group(1)) + 1}'
    else:
        _next_sprint_label = 'the Next Sprint'

    # Status group helpers
    done_g = m['status_groups'].get('done', {'count': 0, 'points': 0, 'items': []})
    new_g = m['status_groups'].get('new', {'count': 0, 'points': 0, 'items': []})
    inprog_g = m['status_groups'].get('inprogress', {'count': 0, 'points': 0, 'items': []})
    review_g = m['status_groups'].get('review', {'count': 0, 'points': 0, 'items': []})
    testing_g = m['status_groups'].get('testing', {'count': 0, 'points': 0, 'items': []})

    target_capacity = max(m['done_points'] * 1.1, 10)

    # Progress bar widths
    total = max(m['total_points'], 1)
    done_pct = done_g['points'] / total * 100
    review_pct = review_g['points'] / total * 100
    testing_pct = testing_g['points'] / total * 100
    inprog_pct = inprog_g['points'] / total * 100
    new_pct = new_g['points'] / total * 100
    new_item_pct = new_g['count'] / m['total_items'] if m['total_items'] > 0 else 0

    # Health badge
    if m['health_rating'] == 'HIGH RISK':
        health_css = 'high-risk'
        health_bg = 'var(--danger-bg)'
        health_color = 'var(--danger)'
    elif m['health_rating'] == 'MODERATE RISK':
        health_css = 'moderate-risk'
        health_bg = 'var(--warn-bg)'
        health_color = 'var(--warn)'
    else:
        health_css = 'healthy'
        health_bg = 'var(--positive-bg)'
        health_color = 'var(--positive)'

    # Assignee table
    sorted_assignees = sorted(m['assignee_stats'].items(), key=lambda x: x[1]['items'], reverse=True)

    # Top actions
    actions = generate_top_actions(items, metrics, antipatterns)

    # Sorted items for appendix
    sorted_items = sorted(items, key=lambda i: i['age_days'])

    # Build HTML
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{snum} Health Report - {team_name}</title>
<style>
  :root {{
    --risk-high: #dc2626;
    --risk-moderate: #f59e0b;
    --risk-healthy: #16a34a;
    --accent: #2563eb;
    --accent-light: #dbeafe;
    --positive: #16a34a;
    --positive-bg: #dcfce7;
    --warn: #f59e0b;
    --warn-bg: #fef3c7;
    --danger: #dc2626;
    --danger-bg: #fee2e2;
    --neutral: #6b7280;
    --neutral-bg: #f3f4f6;
    --bg: #ffffff;
    --bg-alt: #f9fafb;
    --border: #e5e7eb;
    --text: #111827;
    --text-secondary: #4b5563;
    --text-muted: #9ca3af;
    --font: 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif;
    --mono: 'SF Mono', 'Fira Code', 'Consolas', monospace;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    font-family: var(--font);
    color: var(--text);
    background: var(--bg-alt);
    line-height: 1.6;
    font-size: 15px;
  }}

  .page-wrapper {{
    max-width: 1320px;
    margin: 0 auto;
    background: var(--bg);
    min-height: 100vh;
    box-shadow: 0 0 40px rgba(0,0,0,0.06);
    display: flex;
    position: relative;
  }}
  .page-main {{
    flex: 1;
    min-width: 0;
  }}

  .report-header {{
    background: linear-gradient(135deg, #1e293b 0%, #334155 100%);
    color: #fff;
    padding: 48px 56px 40px;
  }}
  .report-header h1 {{
    font-size: 28px;
    font-weight: 700;
    margin-bottom: 4px;
    letter-spacing: -0.5px;
  }}
  .report-header .subtitle {{
    font-size: 16px;
    color: #94a3b8;
    font-weight: 400;
  }}
  .header-meta {{
    display: flex;
    gap: 32px;
    margin-top: 20px;
    flex-wrap: wrap;
  }}
  .header-meta-item {{
    font-size: 13px;
    color: #cbd5e1;
  }}
  .header-meta-item strong {{
    color: #fff;
    font-weight: 600;
  }}

  .content {{
    padding: 0 56px 56px;
  }}

  .section {{
    margin-top: 40px;
  }}
  .section-number {{
    font-size: 12px;
    font-weight: 700;
    color: var(--accent);
    text-transform: uppercase;
    letter-spacing: 1.5px;
    margin-bottom: 6px;
  }}
  .section h2 {{
    font-size: 22px;
    font-weight: 700;
    color: var(--text);
    margin-bottom: 16px;
    padding-bottom: 10px;
    border-bottom: 2px solid var(--border);
  }}
  .section h3 {{
    font-size: 17px;
    font-weight: 600;
    color: var(--text);
    margin: 28px 0 12px;
  }}
  .section h4 {{
    font-size: 14px;
    font-weight: 600;
    color: var(--text-secondary);
    margin: 20px 0 8px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}

  .health-badge-wrapper {{
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 8px 0 12px;
  }}
  .health-badge {{
    display: inline-flex;
    align-items: center;
    gap: 10px;
    padding: 12px 24px;
    border-radius: 8px;
    font-size: 18px;
    font-weight: 700;
  }}
  .health-score-label {{
    font-size: 13px;
    color: var(--text-muted);
  }}
  .health-score-label strong {{
    color: var(--text-secondary);
  }}
  .health-badge.high-risk {{
    background: var(--danger-bg);
    color: var(--danger);
    border: 2px solid var(--danger);
  }}
  .health-badge.moderate-risk {{
    background: var(--warn-bg);
    color: var(--warn);
    border: 2px solid var(--warn);
  }}
  .health-badge.healthy {{
    background: var(--positive-bg);
    color: var(--positive);
    border: 2px solid var(--positive);
  }}
  .health-badge .badge-dot {{
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: currentColor;
    animation: pulse 2s infinite;
  }}
  @keyframes pulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.4; }}
  }}

  .kpi-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin: 20px 0;
  }}
  .kpi-card {{
    padding: 20px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--bg);
    position: relative;
    transition: border-color 0.15s;
  }}
  .kpi-card.selected {{
    border-color: var(--accent);
    box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.15);
  }}
  .kpi-info-btn {{
    position: absolute;
    top: 8px;
    right: 8px;
    width: 20px;
    height: 20px;
    border-radius: 50%;
    border: 1.5px solid var(--text-muted);
    background: transparent;
    color: var(--text-muted);
    font-size: 12px;
    font-weight: 700;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    line-height: 1;
    padding: 0;
    transition: all 0.15s;
  }}
  .kpi-info-btn:hover {{
    border-color: var(--accent);
    color: var(--accent);
  }}
  .kpi-card .kpi-value {{
    font-size: 32px;
    font-weight: 700;
    line-height: 1.1;
  }}
  .kpi-card .kpi-label {{
    font-size: 13px;
    color: var(--text-secondary);
    margin-top: 4px;
  }}
  .kpi-card .kpi-sub {{
    font-size: 12px;
    color: var(--text-muted);
    margin-top: 2px;
  }}
  .kpi-card.danger .kpi-value {{ color: var(--danger); }}
  .kpi-card.warn .kpi-value {{ color: var(--warn); }}
  .kpi-card.positive .kpi-value {{ color: var(--positive); }}
  .kpi-card.neutral .kpi-value {{ color: var(--text-secondary); }}
  .kpi-detail {{
    display: none;
    grid-column: 1 / -1;
    background: var(--bg-card);
    border: 1px solid var(--accent);
    border-radius: 8px;
    padding: 16px 20px;
    animation: kpi-detail-in 0.2s ease;
  }}
  .kpi-detail.active {{
    display: block;
  }}
  @keyframes kpi-detail-in {{
    from {{ opacity: 0; }}
    to {{ opacity: 1; }}
  }}
  .kpi-detail h4 {{
    margin: 0 0 8px 0;
    font-size: 13px;
    font-weight: 700;
    color: var(--accent);
  }}
  .kpi-detail .kpi-detail-what {{
    font-size: 13px;
    line-height: 1.6;
    color: var(--text-secondary);
    margin-bottom: 8px;
  }}
  .kpi-detail .kpi-detail-action {{
    font-size: 13px;
    line-height: 1.6;
    color: var(--text-primary);
    font-style: italic;
    margin-bottom: 8px;
  }}
  .kpi-detail .kpi-detail-thresholds {{
    font-size: 12px;
    color: var(--text-muted);
  }}
  .kpi-detail .kpi-detail-scoring {{
    font-size: 11px;
    color: var(--text-muted);
    margin-top: 2px;
    font-style: italic;
  }}

  .progress-bar-container {{
    margin: 20px 0;
  }}
  .progress-bar {{
    height: 28px;
    border-radius: 6px;
    overflow: hidden;
    display: flex;
    background: var(--neutral-bg);
    border: 1px solid var(--border);
  }}
  .progress-bar .seg {{
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    font-weight: 600;
    color: #fff;
    white-space: nowrap;
    transition: width 0.5s ease;
  }}
  .progress-bar .seg.done {{ background: var(--positive); }}
  .progress-bar .seg.review {{ background: var(--accent); }}
  .progress-bar .seg.testing {{ background: #8b5cf6; }}
  .progress-bar .seg.inprog {{ background: var(--warn); }}
  .progress-bar .seg.new {{ background: var(--neutral); }}
  .progress-legend {{
    display: flex;
    gap: 20px;
    margin-top: 8px;
    font-size: 12px;
    color: var(--text-secondary);
    flex-wrap: wrap;
  }}
  .progress-legend span::before {{
    content: '';
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 2px;
    margin-right: 5px;
    vertical-align: middle;
  }}
  .progress-legend .l-done::before {{ background: var(--positive); }}
  .progress-legend .l-review::before {{ background: var(--accent); }}
  .progress-legend .l-testing::before {{ background: #8b5cf6; }}
  .progress-legend .l-inprog::before {{ background: var(--warn); }}
  .progress-legend .l-new::before {{ background: var(--neutral); }}

  table {{
    width: 100%;
    border-collapse: collapse;
    margin: 16px 0;
    font-size: 14px;
  }}
  thead th {{
    text-align: left;
    padding: 10px 14px;
    background: var(--bg-alt);
    border-bottom: 2px solid var(--border);
    font-weight: 600;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-secondary);
    white-space: nowrap;
  }}
  tbody td {{
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
  }}
  tbody tr:hover {{ background: var(--bg-alt); }}

  .status {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.3px;
    white-space: nowrap;
  }}
  .status.resolved {{ background: #dbeafe; color: #1e40af; }}
  .status.review {{ background: #e0e7ff; color: #3730a3; }}
  .status.testing {{ background: #ede9fe; color: #5b21b6; }}
  .status.inprogress {{ background: #fef3c7; color: #92400e; }}
  .status.new {{ background: #f3f4f6; color: #374151; }}

  .issue-key {{
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 600;
    color: var(--accent);
    white-space: nowrap;
    text-decoration: none;
  }}
  a.issue-key:hover {{
    text-decoration: underline;
  }}

  .callout {{
    padding: 16px 20px;
    border-radius: 8px;
    margin: 16px 0;
    border-left: 4px solid;
    font-size: 14px;
  }}
  .callout.positive {{
    background: var(--positive-bg);
    border-color: var(--positive);
  }}
  .callout.warning {{
    background: var(--warn-bg);
    border-color: var(--warn);
  }}
  .callout.danger {{
    background: var(--danger-bg);
    border-color: var(--danger);
  }}
  .callout strong {{
    display: block;
    margin-bottom: 4px;
  }}

  .dimension-card {{
    border: 1px solid var(--border);
    border-radius: 8px;
    margin: 24px 0;
    overflow: hidden;
  }}
  .dimension-header {{
    padding: 16px 20px;
    background: var(--bg-alt);
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 12px;
  }}
  .dimension-header h3 {{
    margin: 0;
    font-size: 16px;
  }}
  .dimension-header .dim-num {{
    background: var(--accent);
    color: #fff;
    width: 28px;
    height: 28px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 13px;
    font-weight: 700;
    flex-shrink: 0;
  }}
  .dimension-body {{
    padding: 20px;
  }}
  .dimension-body ul {{
    margin: 8px 0 8px 20px;
  }}
  .dimension-body li {{
    margin-bottom: 6px;
  }}
  .dimension-body p {{
    margin-bottom: 10px;
  }}

  .antipattern-grid {{
    display: grid;
    grid-template-columns: 1fr;
    gap: 12px;
    margin: 16px 0;
  }}
  .antipattern-card {{
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 20px;
    border-left: 4px solid var(--danger);
  }}
  .antipattern-card .ap-name {{
    font-weight: 700;
    font-size: 14px;
    color: var(--danger);
    margin-bottom: 6px;
  }}
  .antipattern-card .ap-evidence {{
    font-size: 13px;
    color: var(--text-secondary);
    margin-bottom: 6px;
  }}
  .antipattern-card .ap-impact {{
    font-size: 13px;
    font-style: italic;
    color: var(--text-muted);
  }}

  .action-grid {{
    display: grid;
    grid-template-columns: 1fr;
    gap: 12px;
    margin: 16px 0;
  }}
  .action-card {{
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 20px;
    display: grid;
    grid-template-columns: 40px 1fr;
    gap: 16px;
    align-items: start;
  }}
  .action-card .action-num {{
    width: 36px;
    height: 36px;
    border-radius: 50%;
    background: var(--accent);
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 16px;
    font-weight: 700;
    flex-shrink: 0;
  }}
  .action-card .action-title {{
    font-weight: 700;
    font-size: 14px;
    margin-bottom: 4px;
  }}
  .action-card .action-impact {{
    font-size: 13px;
    color: var(--text-secondary);
    margin-bottom: 4px;
  }}
  .action-card .action-evidence {{
    font-size: 12px;
    color: var(--text-muted);
    font-style: italic;
  }}

  .coaching-card {{
    border: 1px solid var(--border);
    border-radius: 8px;
    margin: 16px 0;
    overflow: hidden;
  }}
  .coaching-card-header {{
    padding: 14px 20px;
    background: var(--bg-alt);
    border-bottom: 1px solid var(--border);
    font-weight: 600;
    font-size: 15px;
  }}
  .coaching-card-body {{
    padding: 16px 20px;
  }}
  .coaching-card-body ul {{
    margin: 8px 0 8px 20px;
  }}
  .coaching-card-body li {{
    margin-bottom: 6px;
    font-size: 14px;
  }}
  .coaching-card-body ol {{
    margin: 8px 0 8px 20px;
  }}
  .coaching-card-body ol li {{
    margin-bottom: 4px;
    font-size: 14px;
  }}
  .coaching-card-body p {{
    margin-bottom: 10px;
    font-size: 14px;
  }}

  .flow-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 16px;
    margin: 16px 0;
  }}
  .flow-card {{
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 20px;
  }}
  .flow-card h4 {{
    margin: 0 0 10px;
    font-size: 14px;
    color: var(--accent);
    text-transform: none;
    letter-spacing: 0;
  }}
  .flow-card ul {{
    margin: 0 0 0 18px;
    font-size: 13px;
  }}
  .flow-card li {{
    margin-bottom: 6px;
  }}

  .appendix-table {{
    font-size: 12px;
    overflow-x: auto;
  }}
  .appendix-table table {{
    min-width: 900px;
  }}
  .appendix-table td, .appendix-table th {{
    padding: 8px 10px;
  }}
  .appendix-table .notes-col {{
    font-size: 11px;
    color: var(--text-secondary);
    max-width: 200px;
  }}

  .sprint-pills {{
    display: flex;
    gap: 3px;
    flex-wrap: wrap;
  }}
  .sprint-pill {{
    display: inline-block;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 10px;
    font-weight: 600;
    background: var(--neutral-bg);
    color: var(--text-secondary);
    white-space: nowrap;
  }}
  .sprint-pill.current {{
    background: var(--accent-light);
    color: var(--accent);
  }}

  .toc-sidebar {{
    width: 200px;
    flex-shrink: 0;
    position: relative;
  }}
  .toc {{
    position: sticky;
    top: 24px;
    padding: 20px 16px;
    font-size: 13px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    max-height: calc(100vh - 48px);
    overflow-y: auto;
  }}
  .toc strong {{
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text-muted);
    margin-bottom: 8px;
    padding-left: 10px;
  }}
  .toc a {{
    color: var(--text-secondary);
    text-decoration: none;
    padding: 5px 10px;
    border-radius: 4px;
    border-left: 2px solid transparent;
    transition: all 0.15s;
    font-size: 12px;
    line-height: 1.4;
  }}
  .toc a:hover {{
    color: var(--accent);
    background: var(--bg-alt);
  }}
  .toc a.active {{
    color: var(--accent);
    border-left-color: var(--accent);
    background: var(--bg-alt);
    font-weight: 600;
  }}

  .btn-print {{
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 8px;
    padding: 12px 20px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    z-index: 100;
    font-family: var(--font);
  }}
  .btn-print:hover {{ background: #1d4ed8; }}

  .report-footer {{
    padding: 24px 56px;
    border-top: 1px solid var(--border);
    font-size: 13px;
    color: var(--text-muted);
    font-style: italic;
    line-height: 1.5;
    background: var(--bg-alt);
  }}

  @media print {{
    body {{ background: #fff; font-size: 12px; }}
    .page-wrapper {{ box-shadow: none; max-width: none; }}
    .report-header {{ padding: 24px 32px; }}
    .content {{ padding: 0 32px 32px; }}
    .badge-dot {{ animation: none !important; }}
    .section {{ page-break-inside: avoid; page-break-before: auto; }}
    .dimension-card {{ page-break-inside: avoid; }}
    .toc-sidebar {{ display: none; }}
    .page-wrapper {{ display: block; max-width: none; }}
    .btn-print {{ display: none; }}
    .kpi-info-btn {{ display: none; }}
    .kpi-detail {{ display: none !important; }}
    .report-footer {{ page-break-before: always; }}
    a {{ color: inherit; text-decoration: none; }}
    .antipattern-card, .action-card, .coaching-card {{ page-break-inside: avoid; }}
  }}

  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #1a1a2e;
      --bg-alt: #16213e;
      --border: #374151;
      --text: #e5e7eb;
      --text-secondary: #9ca3af;
      --text-muted: #6b7280;
      --accent: #60a5fa;
      --accent-light: #1e3a5f;
      --positive: #4ade80;
      --positive-bg: #064e3b;
      --warn: #fbbf24;
      --warn-bg: #78350f;
      --danger: #f87171;
      --danger-bg: #7f1d1d;
      --neutral: #9ca3af;
      --neutral-bg: #374151;
    }}
    .report-header {{
      background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
    }}
    .status.resolved {{ background: #1e3a5f; color: #93c5fd; }}
    .status.review {{ background: #312e81; color: #a5b4fc; }}
    .status.testing {{ background: #4c1d95; color: #c4b5fd; }}
    .status.inprogress {{ background: #78350f; color: #fcd34d; }}
    .status.new {{ background: #374151; color: #d1d5db; }}
    tbody tr:hover {{ background: #1e293b; }}
  }}

  @media (max-width: 960px) {{
    .toc-sidebar {{ display: none; }}
    .page-wrapper {{ display: block; max-width: 1100px; }}
  }}
  @media (max-width: 768px) {{
    .report-header {{ padding: 24px; }}
    .content {{ padding: 0 24px 32px; }}
    .report-footer {{ padding: 20px 24px; }}
    .header-meta {{ gap: 16px; }}
    .kpi-grid {{ grid-template-columns: repeat(2, 1fr); }}
  }}
</style>
</head>
<body>
<div class="page-wrapper">

<div class="toc-sidebar">
  <nav class="toc" id="toc">
    <strong>Navigation</strong>
    <a href="#section-1">Executive Summary</a>
    {'<a href="#section-trends">Trends</a>' if len(history) >= 2 else ''}
    <a href="#section-2">Key Observations</a>
    <a href="#section-3">Dimension Analysis</a>
    <a href="#section-4">Anti-Patterns</a>
    <a href="#section-5">Flow Improvement</a>
    <a href="#section-6">Backlog Improvement</a>
    <a href="#section-7">Top Actions</a>
    <a href="#section-8">Coaching Notes</a>
    {'<a href="#section-9">Observations</a>' if observations else ''}
    <a href="#section-appendix">Item Tracker</a>
  </nav>
</div>

<div class="page-main">
<header class="report-header">
  <h1>{snum} Health Report</h1>
  <div class="subtitle">{team_name}</div>
  <div class="header-meta">
    <div class="header-meta-item"><strong>Report Date</strong><br>{REPORT_DATE}</div>
    <div class="header-meta-item"><strong>Sprint</strong><br>{sprint_name or snum} (Current)</div>
    <div class="header-meta-item"><strong>Team Size</strong><br>{m['team_size']} members</div>
    <div class="header-meta-item"><strong>Team</strong><br>{", ".join(a.split("@")[0] for a in sorted(a2 for a2 in m["assignee_stats"].keys() if a2 != "(Unassigned)")[:10])}</div>
  </div>
</header>

<div class="content">

<!-- 1. Executive Summary -->
<div class="section" id="section-1">
  <div class="section-number">Section 1</div>
  <h2>Executive Summary</h2>

  <div class="health-badge-wrapper">
    <div class="health-badge {health_css}">
      <span class="badge-dot"></span>
      {m['health_rating']}
    </div>
    <span class="health-score-label">Score: <strong>{m['risk_score']}</strong>/10+</span>
    <button class="kpi-info-btn" onclick="toggleKpiDetail('health-rating')" title="How is this rating determined?" style="position:static;">i</button>
  </div>
  <div class="kpi-detail" id="kpi-detail-health-rating">
    <h4>How the Health Rating Works</h4>
    <div class="kpi-detail-what">
      The rating is calculated from a <strong>risk score</strong> &mdash; the sum of points across multiple dimensions of sprint health. A higher score means more areas of concern.
    </div>
    <div class="kpi-detail-what" style="margin-top: 8px;">
      <strong>Rating thresholds:</strong><br>
      &#x1f7e2; <strong>HEALTHY</strong> (0&ndash;2): Delivering predictably with good process discipline.<br>
      &#x1f7e1; <strong>MODERATE RISK</strong> (3&ndash;5): Mostly on track but process gaps could compound over time.<br>
      &#x1f534; <strong>HIGH RISK</strong> (6+): Significant delivery problems requiring focused attention.
    </div>
    <div class="kpi-detail-what" style="margin-top: 8px;">
      <strong>What contributes to the score:</strong>
      <table style="font-size: 12px; margin-top: 6px; width: 100%; border-collapse: collapse;">
        <tr style="border-bottom: 1px solid var(--border);"><td style="padding: 3px 8px;">Delivery rate &lt; 50%</td><td style="padding: 3px 8px; text-align:right;">+3</td></tr>
        <tr style="border-bottom: 1px solid var(--border);"><td style="padding: 3px 8px;">Delivery rate 50&ndash;69%</td><td style="padding: 3px 8px; text-align:right;">+2</td></tr>
        <tr style="border-bottom: 1px solid var(--border);"><td style="padding: 3px 8px;">Delivery rate 70&ndash;84%</td><td style="padding: 3px 8px; text-align:right;">+1</td></tr>
        <tr style="border-bottom: 1px solid var(--border);"><td style="padding: 3px 8px;">AC coverage &lt; 30%</td><td style="padding: 3px 8px; text-align:right;">+2</td></tr>
        <tr style="border-bottom: 1px solid var(--border);"><td style="padding: 3px 8px;">AC coverage 30&ndash;69%</td><td style="padding: 3px 8px; text-align:right;">+1</td></tr>
        <tr style="border-bottom: 1px solid var(--border);"><td style="padding: 3px 8px;">3+ zombie items</td><td style="padding: 3px 8px; text-align:right;">+2</td></tr>
        <tr style="border-bottom: 1px solid var(--border);"><td style="padding: 3px 8px;">1&ndash;2 zombie items</td><td style="padding: 3px 8px; text-align:right;">+1</td></tr>
        <tr style="border-bottom: 1px solid var(--border);"><td style="padding: 3px 8px;">&gt; 30% items never started</td><td style="padding: 3px 8px; text-align:right;">+2</td></tr>
        <tr style="border-bottom: 1px solid var(--border);"><td style="padding: 3px 8px;">15&ndash;30% items never started</td><td style="padding: 3px 8px; text-align:right;">+1</td></tr>
        <tr><td style="padding: 3px 8px;">Priority coverage &lt; 30%</td><td style="padding: 3px 8px; text-align:right;">+1</td></tr>
      </table>
    </div>
    <div class="kpi-detail-action" style="margin-top: 10px;">
      Use this as a retro conversation starter: &ldquo;We scored {m['risk_score']} this sprint &mdash; what are the 1&ndash;2 biggest contributors we can address next sprint?&rdquo; The goal is steady improvement toward HEALTHY, not perfection in one sprint.
    </div>
  </div>

  <div class="kpi-grid">
    <div class="kpi-card {'danger' if m['delivery_rate'] < 0.5 else 'warn' if m['delivery_rate'] < 0.85 else 'positive'}" data-kpi="delivery-rate">
      <button class="kpi-info-btn" onclick="toggleKpiDetail('delivery-rate')" title="What does this mean?">i</button>
      <div class="kpi-value">{m['delivery_rate']:.0%}</div>
      <div class="kpi-label">Delivery Rate</div>
      <div class="kpi-sub">{m['done_points']:.0f} of {m['total_points']:.0f} story points</div>
    </div>
    <div class="kpi-card {'danger' if new_item_pct > 0.3 else 'warn' if new_item_pct > 0.15 else 'positive'}" data-kpi="never-started">
      <button class="kpi-info-btn" onclick="toggleKpiDetail('never-started')" title="What does this mean?">i</button>
      <div class="kpi-value">{new_g['count']}</div>
      <div class="kpi-label">Items Never Started</div>
      <div class="kpi-sub">{new_item_pct:.0%} of sprint items ({new_g['points']:.0f} pts)</div>
    </div>
    <div class="kpi-card {'danger' if m['ac_field_rate'] < 0.3 else 'warn' if m['ac_field_rate'] < 0.7 else 'positive'}" data-kpi="ac-coverage">
      <button class="kpi-info-btn" onclick="toggleKpiDetail('ac-coverage')" title="What does this mean?">i</button>
      <div class="kpi-value">{m['ac_field_count']}/{m['total_items']}</div>
      <div class="kpi-label">Acceptance Criteria</div>
      <div class="kpi-sub">AC coverage</div>
    </div>
    <div class="kpi-card {'warn' if m['max_age'] > 90 else 'neutral'}" data-kpi="oldest-item">
      <button class="kpi-info-btn" onclick="toggleKpiDetail('oldest-item')" title="What does this mean?">i</button>
      <div class="kpi-value">{m['max_age']}d</div>
      <div class="kpi-label">Oldest Open Item</div>
      <div class="kpi-sub">{m['oldest_item']['key'] if m['oldest_item'] else '-'}</div>
    </div>
    <div class="kpi-card neutral" data-kpi="cycle-time">
      <button class="kpi-info-btn" onclick="toggleKpiDetail('cycle-time')" title="What does this mean?">i</button>
      <div class="kpi-value">{m['avg_cycle_time']:.0f}d</div>
      <div class="kpi-label">Avg Cycle Time</div>
      <div class="kpi-sub">Range: {m['min_cycle_time']} &ndash; {m['max_cycle_time']} days</div>
    </div>
    <div class="kpi-card {'danger' if m['max_sprint_carry'] >= 4 else 'warn' if m['max_sprint_carry'] >= 2 else 'positive'}" data-kpi="carryover">
      <button class="kpi-info-btn" onclick="toggleKpiDetail('carryover')" title="What does this mean?">i</button>
      <div class="kpi-value">{m['max_sprint_carry']}</div>
      <div class="kpi-label">Max Sprint Carryover</div>
      <div class="kpi-sub">Sprints for longest-carried item</div>
    </div>
  </div>

  <div class="kpi-detail" id="kpi-detail-delivery-rate">
    <h4>Delivery Rate</h4>
    <div class="kpi-detail-what">Percentage of committed story points completed by sprint end. The core measure of sprint commitment reliability.</div>
    <div class="kpi-detail-action">A consistently low rate means the team is over-committing, getting pulled into unplanned work, or hitting unanticipated blockers. The fix is &ldquo;commit to less and finish it.&rdquo;</div>
    <div class="kpi-detail-thresholds">Thresholds: &#x1f7e2; 85%+ &nbsp; &#x1f7e1; 50&ndash;84% &nbsp; &#x1f534; &lt;50%</div>
    <div class="kpi-detail-scoring">Risk score: +1 (70&ndash;84%) &nbsp; +2 (50&ndash;69%) &nbsp; +3 (&lt;50%)</div>
  </div>
  <div class="kpi-detail" id="kpi-detail-never-started">
    <h4>Items Never Started</h4>
    <div class="kpi-detail-what">Percentage of items that remained in &ldquo;New&rdquo; status for the entire sprint &mdash; committed but never picked up.</div>
    <div class="kpi-detail-action">These reveal a disconnect between planning and capacity. The team is treating the sprint backlog like a wish list rather than a commitment. Coach the team to only pull in what they genuinely intend to start.</div>
    <div class="kpi-detail-thresholds">Thresholds: &#x1f7e2; &lt;15% &nbsp; &#x1f7e1; 15&ndash;30% &nbsp; &#x1f534; &gt;30%</div>
    <div class="kpi-detail-scoring">Risk score: +1 (15&ndash;30%) &nbsp; +2 (&gt;30%)</div>
  </div>
  <div class="kpi-detail" id="kpi-detail-ac-coverage">
    <h4>Acceptance Criteria</h4>
    <div class="kpi-detail-what">Percentage of items with acceptance criteria written in their description. Measures definition-of-ready discipline.</div>
    <div class="kpi-detail-action">Without AC, &ldquo;done&rdquo; is subjective. Low coverage leads to rework, mid-item scope creep, and review delays. This is a leading indicator &mdash; fix it and downstream metrics (cycle time, delivery rate) tend to improve.</div>
    <div class="kpi-detail-thresholds">Thresholds: &#x1f7e2; 70%+ &nbsp; &#x1f7e1; 30&ndash;69% &nbsp; &#x1f534; &lt;30%</div>
    <div class="kpi-detail-scoring">Risk score: +1 (30&ndash;69%) &nbsp; +2 (&lt;30%)</div>
  </div>
  <div class="kpi-detail" id="kpi-detail-oldest-item">
    <h4>Oldest Open Item</h4>
    <div class="kpi-detail-what">Age in days of the oldest unfinished item in the sprint. A high number flags stale work that should be descoped or re-evaluated.</div>
    <div class="kpi-detail-action">Old items create cognitive drag &mdash; they clutter the board, distort metrics, and signal that it&rsquo;s acceptable to leave things unfinished. Action: close it, descope it, or break it into something achievable this sprint.</div>
    <div class="kpi-detail-thresholds">Thresholds: &#x1f7e2; &lt;30d &nbsp; &#x1f7e1; 30&ndash;90d &nbsp; &#x1f534; 90d+</div>
  </div>
  <div class="kpi-detail" id="kpi-detail-cycle-time">
    <h4>Avg Cycle Time</h4>
    <div class="kpi-detail-what">Average days from when an item entered the sprint (or was created, if newer) to resolution. Measures how fast work flows through the sprint.</div>
    <div class="kpi-detail-action">High cycle time + high delivery rate = finishing things but slowly (large items). High cycle time + low delivery rate = work getting stuck. Look for WIP overload, blocked queues, or handoff delays.</div>
    <div class="kpi-detail-thresholds">Thresholds: &#x1f7e2; &lt;14d &nbsp; &#x1f7e1; 14&ndash;30d &nbsp; &#x1f534; 30d+</div>
  </div>
  <div class="kpi-detail" id="kpi-detail-carryover">
    <h4>Max Sprint Carryover</h4>
    <div class="kpi-detail-what">The highest number of sprints any single item has been carried through. Identifies the worst &ldquo;zombie&rdquo; &mdash; work that keeps rolling forward without completion.</div>
    <div class="kpi-detail-action">An item carried 4+ sprints usually points to unclear ownership, missing prerequisites, or work that should have been descoped. Find it, ask &ldquo;what&rsquo;s blocking this from being done or removed?&rdquo; &mdash; the root cause often reveals a systemic issue.</div>
    <div class="kpi-detail-thresholds">Thresholds: &#x1f7e2; 1 &nbsp; &#x1f7e1; 2&ndash;3 &nbsp; &#x1f534; 4+</div>
    <div class="kpi-detail-scoring">Risk score: +1 (1&ndash;2 zombies) &nbsp; +2 (3+ zombies)</div>
  </div>

  <div class="progress-bar-container">
    <strong style="font-size:13px; color: var(--text-secondary); margin-bottom:6px; display:block;">Story Points by Status</strong>
    <div class="progress-bar">
      <div class="seg done" style="width:{done_pct:.1f}%" title="Resolved: {done_g['points']:.0f} pts ({done_g['count']} items)">{done_g['points']:.0f} pts</div>
      <div class="seg review" style="width:{review_pct:.1f}%" title="Review: {review_g['points']:.0f} pts ({review_g['count']} items)">{review_g['points']:.0f}</div>
      <div class="seg testing" style="width:{testing_pct:.1f}%" title="Testing: {testing_g['points']:.0f} pts ({testing_g['count']} items)">{testing_g['points']:.0f}</div>
      <div class="seg inprog" style="width:{inprog_pct:.1f}%" title="In Progress: {inprog_g['points']:.0f} pts ({inprog_g['count']} items)">{inprog_g['points']:.0f} pts</div>
      <div class="seg new" style="width:{new_pct:.1f}%" title="New: {new_g['points']:.0f} pts ({new_g['count']} items)">{new_g['points']:.0f} pts</div>
    </div>
    <div class="progress-legend">
      <span class="l-done">Resolved ({done_g['points']:.0f} pts, {done_g['count']} items)</span>
      <span class="l-review">Review ({review_g['points']:.0f} pts, {review_g['count']} items)</span>
      <span class="l-testing">Testing ({testing_g['points']:.0f} pts, {testing_g['count']} items)</span>
      <span class="l-inprog">In Progress ({inprog_g['points']:.0f} pts, {inprog_g['count']} items)</span>
      <span class="l-new">New / Not Started ({new_g['points']:.0f} pts, {new_g['count']} items)</span>
    </div>
  </div>

  <div class="callout recommendation" style="margin-top: 20px; border-left: 4px solid var(--accent); background: linear-gradient(135deg, rgba(37,99,235,0.06), rgba(37,99,235,0.02)); padding: 16px 20px;">
    <strong style="font-size: 14px; color: var(--accent);">#1 Recommended Action</strong>
    <p style="margin: 8px 0 0; font-size: 14px; line-height: 1.5;">{m['top_recommendation']}</p>
  </div>
</div>

'''

    # --- Positive Signals callout ---
    positive_signals = []
    fast_items = [i for i in items if is_done(i['status']) and i['cycle_days'] is not None and i['cycle_days'] < 14]
    if fast_items:
        positive_signals.append(f'{len(fast_items)} items completed with fast cycle times (&lt;2 weeks)')
    if m['done_items'] > 0:
        positive_signals.append(f'{m["done_items"]} items successfully delivered ({m["done_points"]:.0f} story points)')
    feature_types = sum(1 for i in items if is_done(i['status']) and i['type'] in ('Story', 'Feature'))
    if feature_types >= 3:
        positive_signals.append(f'{feature_types} features/stories completed — strong feature delivery')
    small_items = sum(1 for i in items if is_done(i['status']) and i['has_estimate'] and 0 < i['points'] <= 3)
    if small_items >= 5:
        positive_signals.append(f'{small_items} well-decomposed items (1-3 pts) completed')
    if m['onboarding_count'] == 0 and m['real_delivery_rate'] > 0.7:
        positive_signals.append(f'Strong real-work delivery rate: {m["real_delivery_rate"]:.0%}')

    if positive_signals:
        signals_html = ''.join(f'<li>{s}</li>' for s in positive_signals)
        html += f'''
<div class="callout positive" style="margin-top: 24px;">
  <strong>Positive Signals</strong>
  <ul style="margin: 8px 0 0 20px; font-size: 14px;">{signals_html}</ul>
</div>
'''

    # --- Onboarding items note ---
    if m['onboarding_count'] > 0:
        html += f'''
<div class="callout warning" style="margin-top: 12px;">
  <strong>Onboarding / Automation Items Detected</strong>
  {m['onboarding_count']} items appear to be onboarding or automation tasks (bot-created sub-tasks, LDAP groups, etc.).
  Excluding these, the team's real-work delivery rate is <strong>{m['real_delivery_rate']:.0%}</strong> ({m['real_done_points']:.0f} of {m['real_points']:.0f} points across {m['real_item_count']} items).
</div>
'''

    # --- Trend Charts (between Executive Summary and Section 2) ---
    if len(history) >= 2:
        html += generate_trend_section(history)

    # --- Section 2: Key Sprint Observations ---
    html += '''
<!-- 2. Key Sprint Observations -->
<div class="section" id="section-2">
  <div class="section-number">Section 2</div>
  <h2>Key Sprint Observations</h2>

  <table>
    <thead>
      <tr><th>Observation</th><th>Detail</th><th>Impact</th></tr>
    </thead>
    <tbody>
'''
    # Delivery rate row
    impact_text = 'More than two-thirds' if m['delivery_rate'] < 0.34 else 'More than half' if m['delivery_rate'] < 0.5 else 'A significant portion'
    html += f'''      <tr>
        <td><strong>{m['done_points']:.0f} of {m['total_points']:.0f} story points delivered</strong></td>
        <td>{done_g['count']} items resolved. {inprog_g['count']} items ({inprog_g['points']:.0f} pts) still In Progress. {new_g['count']} items ({new_g['points']:.0f} pts) never started</td>
        <td>{impact_text} of committed work is unfinished</td>
      </tr>
'''
    # Zombie items
    for z in m['zombies'][:3]:
        edata = enrichment.get(z['key'], {})
        detail = edata.get('changelog_summary', f"In {z['sprint_count']}+ sprints, {z['age_days']} days old")
        reasons = '; '.join(z['zombie_reasons'])
        html += f'''      <tr>
        <td><strong>Zombie: {_issue_link(z['key'])}</strong></td>
        <td>{detail}</td>
        <td>{reasons}</td>
      </tr>
'''
    # Blocked items
    for b in m['blocked_items'][:2]:
        edata = enrichment.get(b['key'], {})
        detail = edata.get('changelog_summary', f"Blocked by {', '.join(b['blockers'][:2])}")
        html += f'''      <tr>
        <td><strong>Blocked: {_issue_link(b['key'])}</strong></td>
        <td>{detail}</td>
        <td>External dependency creates unpredictable delay</td>
      </tr>
'''
    # Strong deliveries
    fast = sorted([i for i in items if is_done(i['status']) and i['cycle_days'] is not None], key=lambda x: x['cycle_days'])[:3]
    if fast:
        keys = ", ".join(f'{_issue_link(i["key"])} ({i["cycle_days"]}d)' for i in fast)
        html += f'''      <tr>
        <td><strong>Strong deliveries</strong></td>
        <td>{keys} &mdash; fastest cycle times</td>
        <td>Demonstrates team delivers well on right-sized work</td>
      </tr>
'''
    # AC gap
    if m['ac_field_rate'] < 0.5:
        html += f'''      <tr>
        <td><strong>Low AC coverage</strong></td>
        <td>{m['ac_field_count']}/{m['total_items']} items have acceptance criteria.</td>
        <td>No objective standard for "done"; completion depends on verbal agreement</td>
      </tr>
'''

    html += '''    </tbody>
  </table>
</div>

'''

    # --- Section 3: Dimension Analysis ---
    remaining = m['total_points'] - m['done_points']
    remaining_breakdown = ", ".join(f"{v['points']:.0f} {k.title()}" for k, v in m['status_groups'].items() if k != 'done' and v['count'] > 0)
    recent_items = [i for i in items if i['age_days'] <= 14]
    zero_pt_items = [i for i in items if i['has_estimate'] and i['points'] == 0]
    pd = m['point_distribution']
    dist_str = ", ".join(f"{pts:.0f}-pt ({cnt})" for pts, cnt in sorted(pd.items())) if pd else "N/A"
    long_reviews = [i for i in review_g.get('items', []) if i['age_days'] > 30]

    html += f'''
<!-- 3. Dimension Analysis -->
<div class="section" id="section-3">
  <div class="section-number">Section 3</div>
  <h2>Dimension Analysis</h2>

  <!-- 3.1 -->
  <div class="dimension-card">
    <div class="dimension-header">
      <span class="dim-num">1</span>
      <h3>Sprint Commitment Reliability</h3>
    </div>
    <div class="dimension-body">
      <h4>Observations</h4>
      <ul>
        <li>Total commitment: {m['total_points']:.0f} story points across {m['total_items']} items.</li>
        <li>Delivered: {m['done_points']:.0f} points ({m['delivery_rate']:.0%}) across {m['done_items']} items.</li>
        <li>Remaining: {remaining:.0f} points &mdash; {remaining_breakdown}.</li>
        {f"<li>{len([i for i in m['multi_sprint_items'] if not is_done(i['status'])])} unresolved items appear in multiple sprints (carryover candidates).</li>" if [i for i in m['multi_sprint_items'] if not is_done(i['status'])] else (f"<li>{len(m['multi_sprint_items'])} items appear in multiple sprints, but all are now resolved.</li>" if m['multi_sprint_items'] else "")}
        <li>{new_g['count']} items ({new_g['points']:.0f} points) remain in "New" status &mdash; committed but never started.</li>
      </ul>
      <h4>Potential Risks</h4>
      <ul>
        {f"<li>Commitment at {m['total_points']:.0f} pts is ~{m['overcommit_ratio']:.0f}x the team's demonstrated throughput of {m['done_points']:.0f} pts.</li>" if m['overcommit_ratio'] > 2 else ""}
        {f"<li>The next sprint will inherit {m['total_points'] - m['done_points']:.0f} points of unfinished work if the team follows a carry-forward pattern.</li>" if m['delivery_rate'] < 0.7 else "<li>Low carryover risk &mdash; the team completed the vast majority of committed work.</li>" if m['delivery_rate'] >= 0.85 else ""}
      </ul>
      <h4>Coaching Recommendations</h4>
      <ul>
        <li>Establish a sprint capacity target based on historical velocity: ~{target_capacity:.0f} points.</li>
        <li>Use zero-based loading: start empty, pull only what the team can realistically finish.</li>
        <li>Target 85%+ completion rate within 3 sprints.</li>
      </ul>
    </div>
  </div>

  <!-- 3.2 -->
  <div class="dimension-card">
    <div class="dimension-header">
      <span class="dim-num">2</span>
      <h3>Scope Stability</h3>
    </div>
    <div class="dimension-body">
      <h4>Observations</h4>
      <ul>
        <li>{len(recent_items)} items were created within the last 14 days of the sprint, suggesting {"significant " if len(recent_items) > len(items) * 0.2 else ""}ongoing scope addition.</li>
        {f"<li>{len(zero_pt_items)} items at 0 points &mdash; process items not reflected in capacity planning.</li>" if zero_pt_items else ""}
      </ul>
      <h4>Coaching Recommendations</h4>
      <ul>
        <li>Never repurpose existing items. Create new Jira issues for new work.</li>
        <li>Establish a sprint scope freeze after planning. New items require a trade-off: equal-sized work must be removed.</li>
        <li>Tag mid-sprint additions as &lsquo;unplanned&rsquo; to make scope injection visible.</li>
      </ul>
    </div>
  </div>

  <!-- 3.3 -->
  <div class="dimension-card">
    <div class="dimension-header">
      <span class="dim-num">3</span>
      <h3>Flow Efficiency</h3>
    </div>
    <div class="dimension-body">
      <h4>Observations</h4>
      <ul>
        {f"<li><strong>{new_g['count']} items ({new_g['points']:.0f} pts) in &lsquo;New&rsquo; status</strong> &mdash; added to the sprint but never started.</li>" if new_g['count'] > 0 else ""}
        {f"<li>{testing_g['count']} item(s) in Testing ({testing_g['points']:.0f} pts) &mdash; may indicate a testing bottleneck.</li>" if testing_g['count'] > 0 else ""}
        {f"<li>{review_g['count']} item(s) in Review ({review_g['points']:.0f} pts).{f' {len(long_reviews)} have been in review for 30+ days.' if long_reviews else ''}</li>" if review_g['count'] > 0 else ""}
'''

    # Blocked items in flow section
    for b in m['blocked_items'][:2]:
        html += f'        <li><strong>{b["key"]} blocked</strong> by {", ".join(b["blockers"][:2])}.</li>\n'

    # Cycle times
    if m['cycle_times']:
        fast_ct = [(k, ct) for k, ct in m['cycle_times'] if ct < 14]
        med_ct = [(k, ct) for k, ct in m['cycle_times'] if 14 <= ct < 30]
        slow_ct = [(k, ct) for k, ct in m['cycle_times'] if 30 <= ct < 60]
        vslow_ct = [(k, ct) for k, ct in m['cycle_times'] if ct >= 60]
        html += '        <li>Cycle times for completed items:\n          <ul>\n'
        if fast_ct:
            html += f'            <li>Fast (&lt;2 weeks): {", ".join(f"{k} ({ct}d)" for k, ct in fast_ct[:10])}{f" and {len(fast_ct)-10} more" if len(fast_ct) > 10 else ""}</li>\n'
        if med_ct:
            html += f'            <li>Medium (2&ndash;4 weeks): {", ".join(f"{k} ({ct}d)" for k, ct in med_ct[:10])}{f" and {len(med_ct)-10} more" if len(med_ct) > 10 else ""}</li>\n'
        if slow_ct:
            html += f'            <li>Slow (1&ndash;2 months): {", ".join(f"{k} ({ct}d)" for k, ct in slow_ct[:10])}</li>\n'
        if vslow_ct:
            html += f'            <li>Very slow (2+ months): {", ".join(f"{k} ({ct}d)" for k, ct in vslow_ct[:10])}</li>\n'
        html += '          </ul>\n        </li>\n'

    html += f'''      </ul>
      <h4>Coaching Recommendations</h4>
      <ul>
        <li>Only pull items into the sprint when they will be started within 3 days.</li>
        <li>Implement WIP limits: 2&ndash;3 active items per engineer.</li>
        <li>Establish a review SLA: items in Review for more than 3 business days should be escalated.</li>
      </ul>
    </div>
  </div>

  <!-- 3.4 -->
  <div class="dimension-card">
    <div class="dimension-header">
      <span class="dim-num">4</span>
      <h3>Story Size &amp; Work Decomposition</h3>
    </div>
    <div class="dimension-body">
      <h4>Observations</h4>
      <ul>
        <li>Story point distribution: {dist_str}.</li>
        {f"<li>{m['unestimated_count']} items have no estimate.</li>" if m['unestimated_count'] > 0 else ""}
        {f"<li>{m['zero_point_count']} items at 0 points represent unestimated or placeholder work.</li>" if m['zero_point_count'] > 0 else ""}
      </ul>
      <h4>Coaching Recommendations</h4>
      <ul>
        <li>Every item in the sprint must have a non-zero estimate.</li>
        <li>Items over 5 points should be broken down before entering a sprint.</li>
      </ul>
    </div>
  </div>

  <!-- 3.5 -->
  <div class="dimension-card">
    <div class="dimension-header">
      <span class="dim-num">5</span>
      <h3>Work Distribution</h3>
    </div>
    <div class="dimension-body">
      <h4>Observations</h4>
      <table>
        <thead><tr><th>Assignee</th><th>Items</th><th>Points</th><th>Completed</th><th>Completion Rate</th></tr></thead>
        <tbody>
'''

    for assignee, stats in sorted_assignees:
        comp_rate = f"{stats['done_items']/stats['items']*100:.0f}%" if stats['items'] > 0 else "0%"
        html += f'          <tr><td>{assignee}</td><td>{stats["items"]}</td><td>{stats["points"]:.0f}</td><td>{stats["done_items"]} ({stats["done_points"]:.0f} pts)</td><td>{comp_rate}</td></tr>\n'

    max_items = sorted_assignees[0][1]['items'] if sorted_assignees else 0
    html += f'''        </tbody>
      </table>
      <h4>Coaching Recommendations</h4>
      <ul>
        {f"<li>Redistribute work. {sorted_assignees[0][0]} has {max_items} items &mdash; cap at 3&ndash;4 per person.</li>" if max_items > 5 and sorted_assignees else ""}
        <li>Investigate team members with 0 completions &mdash; are they blocked or working on untracked items?</li>
        <li>For the next sprint, cap individual commitment at 3&ndash;4 items per person.</li>
      </ul>
    </div>
  </div>

  <!-- 3.6 -->
  <div class="dimension-card">
    <div class="dimension-header">
      <span class="dim-num">6</span>
      <h3>Blocker Analysis</h3>
    </div>
    <div class="dimension-body">
      <h4>Observations</h4>
'''

    if m['blocked_items']:
        html += '      <ul>\n'
        for b in m['blocked_items']:
            edata = enrichment.get(b['key'], {})
            detail = edata.get('changelog_summary', f"Blocked by {', '.join(b['blockers'])}")
            html += f'        <li><strong>{_issue_link(b["key"])}</strong> &mdash; {b["summary"][:60]}. {detail}.</li>\n'
        html += '      </ul>\n'
    else:
        html += '      <p>No formal blocker links detected. Jira enrichment may reveal additional blockers from changelogs and comments.</p>\n'

    if m['zombies']:
        html += '      <h4>Zombie Items</h4>\n      <ul>\n'
        for z in m['zombies']:
            edata = enrichment.get(z['key'], {})
            detail = edata.get('changelog_summary', "; ".join(z['zombie_reasons']))
            html += f'        <li><strong><span class="issue-key">{z["key"]}</span></strong> &mdash; {z["summary"][:60]}. {detail}.</li>\n'
        html += '      </ul>\n'

    html += f'''      <h4>Coaching Recommendations</h4>
      <ul>
        <li>Track external dependencies on a separate board. Items blocked by other teams should not count against sprint capacity.</li>
        <li>Establish a maximum sprint lifespan: any item in 2 consecutive sprints must be re-evaluated.</li>
      </ul>
    </div>
  </div>

  <!-- 3.7 -->
  <div class="dimension-card">
    <div class="dimension-header">
      <span class="dim-num">7</span>
      <h3>Backlog Health</h3>
    </div>
    <div class="dimension-body">
      <h4>Observations</h4>
      <ul>
        <li><strong>AC coverage:</strong> {m['ac_field_count']}/{m['total_items']} items have acceptance criteria.</li>
        <li><strong>Priority coverage:</strong> {m['priority_defined_count']}/{m['total_items']} items have defined priority ({m['priority_defined_rate']:.0%}).</li>
        <li><strong>Issue types:</strong> {", ".join(f"{k} ({v})" for k, v in sorted(m['type_distribution'].items(), key=lambda x: -x[1]))}</li>
      </ul>
'''

    # Priority distribution horizontal bar chart
    pri_colors = {'Critical': '#dc2626', 'Blocker': '#dc2626', 'Major': '#f59e0b', 'Normal': '#2563eb', 'Minor': '#6b7280', 'Trivial': '#9ca3af'}
    pri_dist = m.get('priority_distribution', {})
    total_pri = max(sum(pri_dist.values()), 1)
    html += '      <h4>Priority Distribution</h4>\n'
    html += '      <div style="margin: 8px 0 16px;">\n'
    for pname in ['Critical', 'Blocker', 'Major', 'Normal', 'Minor', 'Trivial', 'Undefined']:
        pcount = pri_dist.get(pname, 0)
        if pcount == 0:
            continue
        pct = pcount / total_pri * 100
        pcolor = pri_colors.get(pname, '#d1d5db')
        html += f'        <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 4px; font-size: 13px;">\n'
        html += f'          <span style="width: 70px; text-align: right; color: var(--text-secondary);">{pname}</span>\n'
        html += f'          <div style="flex: 1; height: 18px; background: var(--neutral-bg); border-radius: 3px; overflow: hidden;">\n'
        html += f'            <div style="width: {pct:.1f}%; height: 100%; background: {pcolor}; border-radius: 3px; display: flex; align-items: center; padding-left: 6px; color: #fff; font-size: 11px; font-weight: 600;">{pcount}</div>\n'
        html += f'          </div>\n'
        html += f'        </div>\n'
    html += '      </div>\n'

    html += f'''      <h4>Coaching Recommendations</h4>
      <ul>
        <li>Establish a &lsquo;no AC, no sprint&rsquo; gate: items without testable acceptance criteria cannot enter the sprint.</li>
        <li>Set priority on all items at minimum (Critical/Major/Normal).</li>
        <li>Use issue types deliberately: Story = feature, Bug = defect, Task = technical, Spike = investigation.</li>
      </ul>
    </div>
  </div>

  <!-- 3.8 -->
  <div class="dimension-card">
    <div class="dimension-header">
      <span class="dim-num">8</span>
      <h3>Delivery Predictability</h3>
    </div>
    <div class="dimension-body">
      <h4>Observations</h4>
      <ul>
        <li>Completed points ({m['done_points']:.0f}) vs. committed ({m['total_points']:.0f}) = {m['delivery_rate']:.0%} delivery rate.</li>
        {f"<li>Average cycle time: {m['avg_cycle_time']:.0f} days. Median: {m['median_cycle_time']:.0f} days. Range: {m['min_cycle_time']}&ndash;{m['max_cycle_time']} days.</li>" if m['cycle_times'] else ""}
        {f"<li>{len(m['multi_sprint_items'])} items appear in multiple sprints, indicating a chronic carryover pattern.</li>" if m['multi_sprint_items'] else ""}
      </ul>
      <h4>Coaching Recommendations</h4>
      <ul>
        <li>Adopt a &lsquo;fresh sprint&rsquo; policy: incomplete items return to backlog at sprint boundary.</li>
        <li>Target commitment of ~{target_capacity:.0f} points (based on current {m['done_points']:.0f}-point throughput).</li>
        <li>Use cycle time &gt; 2 sprints as a health flag for investigation.</li>
      </ul>
    </div>
  </div>

</div>

'''

    # --- Section 4: Anti-Patterns ---
    html += '''<!-- 4. Anti-Patterns -->
<div class="section" id="section-4">
  <div class="section-number">Section 4</div>
  <h2>Agile Anti-Patterns Detected</h2>

  <div class="antipattern-grid">
'''
    # Anti-pattern cards
    for ap in antipatterns:
        html += f'''    <div class="antipattern-card">
      <div class="ap-name">{ap['name']}</div>
      <div class="ap-evidence">{ap['evidence']}</div>
      <div class="ap-impact">{ap['impact']}</div>
    </div>
'''

    if not antipatterns:
        html += '    <p>No significant anti-patterns detected.</p>\n'

    html += '''  </div>
</div>

'''

    # --- Section 5: Flow Improvement Opportunities ---
    long_running = [i for i in items if not is_done(i['status']) and i['age_days'] > 45]
    near_done = [i for i in items if i['status_cat'] in ('review', 'testing')]
    html += f'''
<!-- 5. Flow Improvement Opportunities -->
<div class="section" id="section-5">
  <div class="section-number">Section 5</div>
  <h2>Flow Improvement Opportunities</h2>

  <h3>Cycle Time Reduction</h3>
  <ul>
    {f"<li><strong>Remove sprint queue time.</strong> {new_g['count']} items in &lsquo;New&rsquo; were never started. Only add items when the team has capacity to start within 3 days.</li>" if new_g['count'] > 0 else ""}
    <li><strong>Implement WIP limits</strong> (2&ndash;3 active items per engineer).</li>
'''
    if long_running:
        lr_keys = ", ".join(f"{i['key']} ({i['age_days']}d)" for i in long_running[:3])
        html += f'    <li><strong>Address long-running items.</strong> {lr_keys} need focused completion or re-scoping.</li>\n'

    html += '''  </ul>

  <h3>Throughput Improvement</h3>
  <ul>
'''
    if near_done:
        nd_keys = ", ".join(f"{i['key']} ({i['points']:.0f}pts, {i['status']})" for i in near_done[:4])
        html += f'    <li><strong>Focus on items closest to Done.</strong> {nd_keys} &mdash; prioritize reviewer/tester time.</li>\n'
    if m['zombies']:
        z_keys = ", ".join(f"{z['key']}" for z in m['zombies'][:3])
        html += f'    <li><strong>Remove zombie items.</strong> {z_keys} should exit the sprint immediately.</li>\n'

    html += f'''  </ul>

  <h3>Delivery Predictability</h3>
  <ul>
    <li><strong>Right-size commitment</strong> to ~{target_capacity:.0f} points with a 10% unplanned buffer.</li>
    <li><strong>Track external dependencies separately.</strong> Items blocked by other teams on a dependency board.</li>
    <li><strong>Track flow metrics:</strong> completion ratio, cycle time, WIP count, carryover rate.</li>
  </ul>
</div>

'''

    # --- Section 6: Backlog Improvement Opportunities ---
    html += '''
<!-- 6. Backlog Improvement Opportunities -->
<div class="section" id="section-6">
  <div class="section-number">Section 6</div>
  <h2>Backlog Improvement Opportunities</h2>

  <h3>Structural Issues</h3>
  <ol>
'''
    if m['ac_field_rate'] < 0.5:
        html += f'    <li><strong>Low acceptance criteria coverage</strong> &mdash; {m["ac_field_count"]}/{m["total_items"]} items have acceptance criteria.</li>\n'
    if m['priority_defined_rate'] < 0.5:
        html += f'    <li><strong>Priority field underused</strong> &mdash; {100 - m["priority_defined_rate"]*100:.0f}% of items have &lsquo;Undefined&rsquo; priority.</li>\n'
    if len(m['type_distribution']) <= 2:
        dom_type = max(m['type_distribution'], key=m['type_distribution'].get)
        html += f'    <li><strong>Issue type uniformity</strong> &mdash; {m["type_distribution"][dom_type]} of {m["total_items"]} items are {dom_type}s.</li>\n'
    if m['zero_point_count'] > 0:
        html += f'    <li><strong>Unestimated work</strong> &mdash; {m["zero_point_count"]} items at 0 points.</li>\n'

    html += '''  </ol>

  <h3>Recommendations</h3>
  <ul>
    <li><strong>Adopt a standard description template.</strong> Overview, Technical Details, Acceptance Criteria (checklist), Dependencies.</li>
    <li><strong>Establish a &lsquo;no AC, no sprint&rsquo; gate.</strong> Every item must have testable acceptance criteria.</li>
    <li><strong>Use issue types deliberately.</strong> Story = feature, Bug = defect, Task = technical, Spike = investigation.</li>
    <li><strong>Conduct a backlog cleanup session (60 min):</strong> remove items older than 3 sprints, add AC to top 15 items, estimate all items, set priorities.</li>
    <li><strong>Two-sprint expiry rule.</strong> No automatic carryover &mdash; re-scope, re-assign, or remove.</li>
  </ul>
</div>
'''

    html += f'''
<!-- 7. Top 5 Actions -->
<div class="section" id="section-7">
  <div class="section-number">Section 7</div>
  <h2>Top 5 Actions for {_next_sprint_label}</h2>

  <div class="action-grid">
'''
    # Action cards
    for i, action in enumerate(actions[:5], 1):
        html += f'''    <div class="action-card">
      <div class="action-num">{i}</div>
      <div>
        <div class="action-title">{action['title']}</div>
        <div class="action-impact">{action['impact']}</div>
        <div class="action-evidence">{action['evidence']}</div>
      </div>
    </div>
'''

    html += '''  </div>
</div>

<!-- 8. Coaching Notes -->
<div class="section" id="section-8">
  <div class="section-number">Section 8</div>
  <h2>Agile Coaching Notes</h2>

  <div class="coaching-card">
    <div class="coaching-card-header">For the Sprint Retrospective</div>
    <div class="coaching-card-body">
      <p><strong>Suggested focus areas:</strong></p>
      <ul>
'''
    html += f'        <li>"We committed {m["total_points"]:.0f} points and delivered {m["done_points"]:.0f}. What would a sprint look like where we complete 85% of what we commit?"</li>\n'
    if m['zombies']:
        z = m['zombies'][0]
        html += f'        <li>"{z["key"]} has been in our sprint for {z["sprint_count"]}+ sprints. What would it take to either define it or remove it?"</li>\n'
    html += f'''      </ul>
      <p><strong>Facilitation tips:</strong></p>
      <ul>
        <li>Frame the {m['delivery_rate']:.0%} completion rate as a <em>planning</em> problem, not a <em>performance</em> problem.</li>
        <li>Lead with the positive: highlight completed items and successful delivery patterns.</li>
      </ul>
    </div>
  </div>

  <div class="coaching-card">
    <div class="coaching-card-header">For Sprint Planning</div>
    <div class="coaching-card-body">
      <p><strong>Key principles:</strong></p>
      <ul>
        <li><strong>Zero-based loading:</strong> Start with an empty sprint. Pull items deliberately.</li>
        <li><strong>Capacity:</strong> ~{target_capacity:.0f} points. Subtract 3&ndash;5 for unplanned work.</li>
        <li><strong>Readiness gate:</strong> Every item must have AC, estimate (&gt;0), owner, and priority.</li>
        <li><strong>Dependency check:</strong> Blocked items enter only when unblocked.</li>
      </ul>
    </div>
  </div>

  <div class="coaching-card">
    <div class="coaching-card-header">For Backlog Refinement</div>
    <div class="coaching-card-body">
      <p><strong>Session structure (60 min):</strong></p>
      <ol>
        <li><strong>Cleanup (15 min):</strong> Remove items older than 3 sprints. Review all zombie items.</li>
        <li><strong>AC writing (20 min):</strong> Add testable acceptance criteria to top 15 backlog items.</li>
        <li><strong>Estimation (15 min):</strong> Estimate all items entering next sprint. No 0-point items.</li>
        <li><strong>Priority setting (10 min):</strong> Set priority on all unset items. Use Critical/Major/Normal only.</li>
      </ol>
      <p><strong>Definition of Ready checklist:</strong></p>
      <ul>
        <li>Has testable acceptance criteria</li>
        <li>Has non-zero story point estimate</li>
        <li>Has an assigned owner</li>
        <li>Has priority set (not Undefined)</li>
        <li>Has no unresolved external dependencies</li>
      </ul>
    </div>
  </div>
</div>

'''

    # --- 9. Additional Observations (HTML) ---
    if observations:
        html += '''
<!-- 9. Additional Observations -->
<div class="section" id="section-9">
  <div class="section-number">Section 9</div>
  <h2>Additional Observations</h2>
  <p style="color:#64748b; margin-bottom:1.5rem;">The following patterns were detected from enrichment data (changelogs, comments, sprint history) and may warrant discussion.</p>
'''
        for obs in observations:
            sev_color = '#f59e0b' if obs['severity'] == 'warning' else '#3b82f6'
            sev_label = 'Warning' if obs['severity'] == 'warning' else 'Info'
            sev_bg = '#fef3c7' if obs['severity'] == 'warning' else '#dbeafe'
            items_html = ''
            if obs.get('items'):
                item_links = ', '.join(_issue_link(k) for k in obs['items'])
                items_html = f'<p style="margin-top:0.5rem;font-size:0.85rem;color:#64748b;">Affected: {item_links}</p>'
            html += f'''
  <div style="border-left:4px solid {sev_color}; background:{sev_bg}; padding:1rem 1.25rem; border-radius:8px; margin-bottom:1rem;">
    <div style="display:flex; align-items:center; gap:0.5rem; margin-bottom:0.5rem;">
      <span style="background:{sev_color}; color:white; font-size:0.7rem; padding:2px 8px; border-radius:4px; font-weight:600;">{sev_label}</span>
      <strong>{obs['title']}</strong>
    </div>
    <p style="margin:0; color:#334155;">{obs['detail']}</p>
    {items_html}
  </div>
'''
        html += '</div>\n'

    html += '''
<!-- Appendix -->
<div class="section" id="section-appendix">
  <div class="section-number">Appendix</div>
  <h2>Sprint Item Tracker</h2>

  <div class="appendix-table" style="overflow-x: auto;">
    <table>
      <thead>
        <tr>
          <th>Issue Key</th>
          <th>Type</th>
          <th>Status</th>
          <th>Pts</th>
          <th>Assignee</th>
          <th>Age</th>
          <th>Sprint History</th>
          <th>Notes</th>
        </tr>
      </thead>
      <tbody>
'''
    # Appendix rows
    for item in sorted_items:
        status_css = {
            'done': 'resolved', 'review': 'review', 'testing': 'testing',
            'inprogress': 'inprogress', 'new': 'new'
        }.get(item['status_cat'], 'new')

        # Sprint pills
        if item['sprint_labels']:
            last_sprint = item['sprint_labels'][-1]
            pills = "".join(
                f'<span class="sprint-pill{" current" if s == last_sprint else ""}">{s}</span>'
                for s in item['sprint_labels']
            )
            sprint_html = f'<div class="sprint-pills">{pills}</div>'
        else:
            sprint_html = '-'

        # Notes
        notes_parts = []
        if is_done(item['status']) and item['cycle_days']:
            notes_parts.append(f"completed ({item['cycle_days']}d)")
        edata = enrichment.get(item['key'], {})
        if edata.get('changelog_summary'):
            notes_parts.append(edata['changelog_summary'][:80])
        else:
            notes_parts.append(item['summary'][:60])
        notes = "; ".join(notes_parts)

        html += f'''        <tr>
          <td>{_issue_link(item['key'])}</td>
          <td>{item['type']}</td>
          <td><span class="status {status_css}">{item['status']}</span></td>
          <td>{item['points']:.0f}</td>
          <td>{item['assignee']}</td>
          <td>{item['age_days']}d</td>
          <td>{sprint_html}</td>
          <td class="notes-col">{notes}</td>
        </tr>
'''

    html += '''      </tbody>
    </table>
  </div>
</div>

</div><!-- /content -->

<footer class="report-footer">
  This report is intended to support the team's continuous improvement journey. The observations and recommendations are systemic in nature and should be discussed collaboratively. The goal is not to assign blame but to identify process improvements that enable the team to deliver more predictably, with higher quality, and with less stress.
</footer>

</div><!-- /page-main -->
</div><!-- /page-wrapper -->

<button class="btn-print" onclick="window.print()" title="Export to PDF via Print">Export PDF</button>

<script>
function toggleKpiDetail(kpiId) {
  var panel = document.getElementById('kpi-detail-' + kpiId);
  var card = document.querySelector('[data-kpi="' + kpiId + '"]');
  var wasActive = panel.classList.contains('active');
  // Close all open panels and deselect all cards
  document.querySelectorAll('.kpi-detail.active').forEach(function(p) {
    p.classList.remove('active');
  });
  document.querySelectorAll('.kpi-card.selected').forEach(function(c) {
    c.classList.remove('selected');
  });
  // Toggle: if it wasn't active, open it
  if (!wasActive) {
    panel.classList.add('active');
    card.classList.add('selected');
  }
}

// Scroll-spy: highlight active section in sidebar TOC
(function() {
  var tocLinks = document.querySelectorAll('.toc a[href^="#section"]');
  if (!tocLinks.length) return;
  var sections = [];
  tocLinks.forEach(function(link) {
    var id = link.getAttribute('href').substring(1);
    var el = document.getElementById(id);
    if (el) sections.push({ el: el, link: link });
  });
  function onScroll() {
    var scrollY = window.scrollY + 80;
    var current = null;
    for (var i = 0; i < sections.length; i++) {
      if (sections[i].el.offsetTop <= scrollY) {
        current = sections[i];
      }
    }
    tocLinks.forEach(function(l) { l.classList.remove('active'); });
    if (current) current.link.classList.add('active');
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();
})();
</script>

</body>
</html>'''

    return html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Sprint Health Analyzer -- Generate Agile sprint health reports from Jira CSV exports.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Basic analysis from CSV
  python3 sprint_health_analyzer.py --csv sprint.csv --sprint "Sprint NN" --team "Team Name"

  # Analyze directly from Jira using sprint ID
  python3 sprint_health_analyzer.py --sprintid 12345

  # Analyze from Jira MCP tool JSON output
  python3 sprint_health_analyzer.py --jira-json mcp_output.json --sprint "Sprint NN" --team "Team Name"

  # Analyze CSV with sprint ID for matching
  python3 sprint_health_analyzer.py --csv sprint.csv --sprintid 12345 --team "Team Name"

  # Generate enrichment request list
  python3 sprint_health_analyzer.py --csv sprint.csv --sprint "Sprint NN" --team "Team Name" --enrichment-requests

  # Full analysis with Jira enrichment
  python3 sprint_health_analyzer.py --csv sprint.csv --sprint "Sprint NN" --team "Team Name" --enrichment enrichment.json
        '''
    )
    parser.add_argument('--csv', default=None, help='Path to Jira CSV export file')
    parser.add_argument('--jira-json', default=None, help='Path to JSON file from Jira MCP tools (jira_get_sprint_issues or jira_search output)')
    parser.add_argument('--sprint', default=None, help='Sprint name or number to analyze (e.g., "Sprint 26" or "26")')
    parser.add_argument('--sprintid', default=None, type=int, help='Jira sprint ID (integer). Fetches issues directly from Jira REST API.')
    parser.add_argument('--jira-url', default=None, help='Jira server URL (or set JIRA_URL env var)')
    parser.add_argument('--jira-user', default=None, help='Jira username/email (or set JIRA_USER env var)')
    parser.add_argument('--jira-token', default=None, help='Jira API token (or set JIRA_TOKEN env var)')
    parser.add_argument('--team', default=None, help='Team name (auto-detected from CSV if not provided)')
    parser.add_argument('--enrichment', default=None, help='Path to Jira enrichment JSON file')
    parser.add_argument('--enrichment-requests', action='store_true', help='Output enrichment request list and exit')
    parser.add_argument('--output', default='.', help='Output directory for reports (default: current directory)')
    parser.add_argument('--history', default=None, help='Path to sprint history JSON file (accumulates metrics across runs for trend charts)')
    parser.add_argument('--date', default=None, help='Report date override (default: today)')

    args = parser.parse_args()

    # Validate: need at least one data source
    if not args.csv and not args.sprintid and not args.jira_json:
        parser.error("Either --csv, --jira-json, or --sprintid is required.")

    if args.date:
        global REPORT_DATE
        REPORT_DATE = args.date

    # Fetch or parse data
    if args.jira_json:
        # Parse from Jira MCP tool JSON output
        sprint_arg = args.sprint or (str(args.sprintid) if args.sprintid else None)
        print(f"Parsing MCP JSON: {args.jira_json}")
        items, detected_team, detected_sprint = parse_mcp_json(args.jira_json, sprint_arg)
    elif args.sprintid and not args.csv:
        # Fetch directly from Jira REST API
        jira_url = args.jira_url or os.environ.get('JIRA_URL', '')
        jira_user = args.jira_user or os.environ.get('JIRA_USER', '')
        jira_token = args.jira_token or os.environ.get('JIRA_TOKEN', '')

        if not jira_url or not jira_user or not jira_token:
            print("ERROR: Jira credentials required when using --sprintid without --csv.")
            print()
            print("Provide credentials via arguments or environment variables:")
            print("  --jira-url URL    or  export JIRA_URL=https://issues.redhat.com")
            print("  --jira-user USER  or  export JIRA_USER=you@example.com")
            print("  --jira-token TOK  or  export JIRA_TOKEN=your-api-token")
            sys.exit(1)

        print(f"Fetching sprint {args.sprintid} from {jira_url}...")
        items, detected_team, detected_sprint = fetch_sprint_issues(
            args.sprintid, jira_url, jira_user, jira_token
        )
    else:
        # Parse from CSV
        sprint_arg = args.sprint or str(args.sprintid) if args.sprintid else args.sprint
        if not sprint_arg:
            parser.error("--sprint is required when using --csv without --sprintid.")

        print(f"Parsing CSV: {args.csv}")
        items, detected_team, detected_sprint = parse_csv(args.csv, sprint_arg)

    if not items:
        print(f"ERROR: No items found.")
        if args.csv:
            print("Check that the sprint name matches the Sprint column values in your CSV.")
        elif args.jira_json:
            print("Check that the JSON file contains issues and the sprint name matches.")
        else:
            print(f"Check that sprint ID {args.sprintid} exists and contains issues.")
        sys.exit(1)

    team_name = args.team or detected_team or "Unknown Team"
    sprint_name = detected_sprint or args.sprint or f"Sprint {args.sprintid}"
    sprint_num = extract_sprint_number(sprint_name)

    print(f"Found {len(items)} items for {sprint_name} ({team_name})")

    # Enrichment requests mode
    if args.enrichment_requests:
        requests = generate_enrichment_requests(items)
        output_path = os.path.join(args.output, 'enrichment_requests.json')
        with open(output_path, 'w') as f:
            json.dump(requests, f, indent=2, default=str)

        print(f"\nEnrichment requests written to: {output_path}")
        print(f"Top {min(len(requests), 10)} items to enrich via Jira:")
        for r in requests[:10]:
            print(f"  {r['key']} (score: {r['priority_score']}) -- {', '.join(r['reasons'])}")

        print(f"\nTo enrich, look up each issue in Jira with expand=changelog,")
        print(f"then create an enrichment JSON file with the structure shown in")
        print(f"SPRINT_ANALYSIS_GUIDE.md and re-run with --enrichment <file>.")
        return

    # Load enrichment data
    enrichment = load_enrichment(args.enrichment) if args.enrichment else {}
    if enrichment:
        print(f"Loaded enrichment data for {len(enrichment)} issues")

    # Compute metrics
    print("Computing metrics...")
    metrics = compute_metrics(items, enrichment)

    # Detect anti-patterns
    antipatterns = detect_antipatterns(items, metrics, enrichment)

    # Detect additional observations
    observations = detect_observations(items, metrics, enrichment)

    # Load/save sprint history for trend charts
    history = load_history(args.history) if args.history else []
    if args.history:
        entry_update = {'antipattern_count': len(antipatterns)}
        history = save_history(args.history, history, sprint_name, metrics)
        # Patch antipattern_count on latest entry
        for h in history:
            if h['sprint'] == sprint_name:
                h['antipattern_count'] = len(antipatterns)
        with open(args.history, 'w') as f:
            json.dump(history, f, indent=2)
        print(f"Sprint history:  {args.history} ({len(history)} sprints)")

    # Determine Jira base URL for issue linking
    jira_base_url = args.jira_url or os.environ.get('JIRA_URL', '') or None

    # Generate reports
    os.makedirs(args.output, exist_ok=True)

    # Sanitize sprint name for filename
    safe_name = re.sub(r'[^\w\s-]', '', sprint_num or 'Sprint').replace(' ', '_')

    # Markdown
    md_content = generate_markdown(team_name, sprint_name, sprint_num, items, metrics, antipatterns, enrichment, observations=observations)
    md_path = os.path.join(args.output, f'{safe_name}_Health_Report.md')
    with open(md_path, 'w') as f:
        f.write(md_content)
    print(f"Markdown report: {md_path}")

    # HTML
    html_content = generate_html(team_name, sprint_name, sprint_num, items, metrics, antipatterns, enrichment,
                                  jira_base_url=jira_base_url, history=history, observations=observations)
    html_path = os.path.join(args.output, f'{safe_name}_Health_Report.html')
    with open(html_path, 'w') as f:
        f.write(html_content)
    print(f"HTML report:     {html_path}")

    # Summary
    print(f"\n{'='*60}")
    print(f"Sprint Health: {metrics['health_rating']} (Score: {metrics['risk_score']})")
    print(f"Delivery Rate: {metrics['delivery_rate']:.0%} ({metrics['done_points']:.0f}/{metrics['total_points']:.0f} pts)")
    print(f"Items: {metrics['done_items']}/{metrics['total_items']} completed")
    print(f"Anti-Patterns: {len(antipatterns)} detected")
    print(f"Observations: {len(observations)} additional patterns found")
    print(f"Zombies: {len(metrics['zombies'])} items")
    print(f"AC Coverage: {metrics['ac_field_rate']:.0%}")
    print(f"Avg Cycle Time: {metrics['avg_cycle_time']:.1f}d")
    print(f"\n>> {metrics['top_recommendation_short']}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
