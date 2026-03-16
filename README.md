# Sprint Health Analyzer

Generate comprehensive sprint health reports from your Jira data. Produces a styled HTML report with executive KPI cards, 8-dimension health assessment, anti-pattern detection, and coaching recommendations.

![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue) ![No Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

---

## What You Get

The analyzer produces an **HTML report** (and a Markdown version) with:

- **Executive Summary** -- KPI cards for delivery rate, velocity, cycle time, and overall health score
- **8-Dimension Assessment** -- Sprint commitment, scope stability, flow efficiency, story sizing, work distribution, blockers, backlog health, and predictability
- **Anti-Pattern Detection** -- Automatically flags zombie items, scope creep, estimation issues, and carryover patterns
- **Trend Charts** -- Sprint-over-sprint comparisons when you run it across multiple sprints
- **Coaching Recommendations** -- Actionable suggestions for the team

---

## Quick Start

### Prerequisites

- **Python 3.8 or later** (check with `python3 --version`)
  - [Download Python](https://www.python.org/downloads/) if needed
- No additional packages or installs required

### Step 1: Export Your Sprint Data from Jira

1. Open your Jira board
2. Navigate to the **Backlog** or **Active Sprint** view
3. Click the **...** menu (top right) > **Export** > **CSV (All fields)**
4. Save the downloaded CSV file

### Step 2: Run the Analyzer

**Option A: Interactive mode (recommended for first-time users)**

```bash
./analyze_sprint.sh
```

The script will walk you through each step with prompts.

You can also pass your CSV file directly:

```bash
./analyze_sprint.sh path/to/your-sprint-export.csv
```

**Option B: Direct command**

```bash
python3 sprint_health_analyzer.py \
  --csv "your-sprint-export.csv" \
  --sprint "Sprint 27" \
  --team "Your Team Name" \
  --output ./reports
```

### Step 3: View the Report

Open the generated HTML file in your browser:

```
reports/S27_Health_Report.html
```

---

## Input Options

| Source | Flag | Description |
|--------|------|-------------|
| Jira CSV export | `--csv FILE` | Standard Jira CSV export file |
| Jira MCP JSON | `--jira-json FILE` | JSON output from Jira MCP tools |
| Jira API | `--sprintid ID` | Fetch directly from Jira (requires credentials) |

### Using Jira API Credentials (optional)

To fetch sprint data directly from Jira without exporting CSV:

```bash
export JIRA_URL=https://your-jira-instance.atlassian.net
export JIRA_USER=your-email@company.com
export JIRA_TOKEN=your-api-token

python3 sprint_health_analyzer.py --sprintid 12345 --output ./reports
```

To generate an API token: [Atlassian API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens)

Setting `JIRA_URL` also makes issue keys in the report clickable links to Jira. You can set it even when using CSV mode:

```bash
python3 sprint_health_analyzer.py \
  --csv sprint.csv \
  --sprint "Sprint 27" \
  --team "My Team" \
  --jira-url https://your-jira-instance.atlassian.net
```

---

## Tracking Trends Across Sprints

Run the analyzer on each sprint using the same `--history` file. After 2+ sprints, the report automatically includes trend charts.

```bash
# Sprint 25
python3 sprint_health_analyzer.py --csv sprint25.csv --sprint "25" \
  --team "My Team" --history my_team_history.json --output ./reports

# Sprint 26 (same history file)
python3 sprint_health_analyzer.py --csv sprint26.csv --sprint "26" \
  --team "My Team" --history my_team_history.json --output ./reports
```

The interactive script (`analyze_sprint.sh`) does this automatically -- it creates a per-team history file in the output directory.

---

## Output Files

| File | Description |
|------|-------------|
| `S{NN}_Health_Report.html` | Styled HTML report -- open in any browser |
| `S{NN}_Health_Report.md` | Markdown version of the report |
| `{team}_history.json` | Sprint metrics history for trend tracking |

---

## Full CLI Reference

```
python3 sprint_health_analyzer.py [OPTIONS]

Required (one of):
  --csv FILE           Path to Jira CSV export
  --jira-json FILE     Path to Jira MCP tool JSON output
  --sprintid ID        Jira sprint ID (requires credentials)

Options:
  --sprint NAME        Sprint name or number (e.g., "Sprint 27" or "27")
  --team NAME          Team name for the report header
  --jira-url URL       Jira URL (makes issue keys clickable)
  --enrichment FILE    Jira enrichment data (changelog details)
  --enrichment-requests  Generate list of issues to enrich
  --history FILE       Sprint history file (enables trend charts)
  --output DIR         Output directory (default: current directory)
  --date DATE          Report date override
  -h, --help           Show help message
```

---

## Report Dimensions

| # | Dimension | What It Measures |
|---|-----------|-----------------|
| 1 | Sprint Commitment Reliability | Committed vs. delivered points, carryover patterns |
| 2 | Scope Stability | Mid-sprint scope changes, late additions, repurposing |
| 3 | Flow Efficiency | WIP distribution, queue time, bottlenecks |
| 4 | Story Size & Decomposition | Point distribution, estimation gaps |
| 5 | Work Distribution | Per-person workload and delivery balance |
| 6 | Blocker Analysis | External dependencies, blocked and zombie items |
| 7 | Backlog Health | Acceptance criteria coverage, priority distribution |
| 8 | Delivery Predictability | Completion ratio, cycle time variance |

---

## FAQ

**Q: Do I need to install anything besides Python?**
No. The analyzer uses only Python standard library modules -- no `pip install` required.

**Q: What Python version do I need?**
Python 3.8 or later. Check with `python3 --version`.

**Q: Can I run this on Windows?**
Yes. Use `python3 sprint_health_analyzer.py ...` in PowerShell or Command Prompt. The interactive `analyze_sprint.sh` requires bash (Git Bash, WSL, or similar).

**Q: How do I get the sprint ID for `--sprintid`?**
Open your Jira board, look at the URL. It typically contains `rapidView=` (board ID). You can also find the sprint ID in the sprint dropdown or via the Jira API.

**Q: The report says "HIGH RISK" but my sprint just started. Is that normal?**
Yes. Early in a sprint, delivery rate is naturally low. The risk score reflects the current snapshot. Focus on the anti-patterns and recommendations rather than the overall score during the first few days.

**Q: Can multiple teams use this?**
Yes. Each team should use their own history file for accurate trend tracking. The interactive script handles this automatically.
