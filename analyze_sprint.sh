#!/usr/bin/env bash
# ============================================================================
# Sprint Health Analyzer - Interactive Runner
# ============================================================================
# This script walks you through generating a sprint health report.
# No command-line arguments needed - just run it and follow the prompts.
#
# Prerequisites: Python 3.8+ (no extra packages required)
#
# Usage:
#   ./analyze_sprint.sh
#
# Or with a CSV file:
#   ./analyze_sprint.sh path/to/sprint-export.csv
# ============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANALYZER="${SCRIPT_DIR}/sprint_health_analyzer.py"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m' # No Color

echo ""
echo -e "${BOLD}==========================================${NC}"
echo -e "${BOLD}  Sprint Health Analyzer${NC}"
echo -e "${BOLD}==========================================${NC}"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: Python 3 is required but not installed.${NC}"
    echo "Install it from https://www.python.org/downloads/"
    exit 1
fi

# ---- Step 1: Get the CSV file ----
CSV_FILE="${1:-}"

if [ -z "$CSV_FILE" ]; then
    echo -e "${BLUE}Step 1: Provide your Jira CSV export file${NC}"
    echo ""
    echo "  How to export from Jira:"
    echo "  1. Open your Jira board"
    echo "  2. Go to the Backlog or Sprint view"
    echo "  3. Click '...' menu > Export > CSV (All fields)"
    echo "  4. Save the file"
    echo ""
    read -r -p "Path to CSV file: " CSV_FILE
    echo ""
fi

# Handle drag-and-drop paths (remove quotes)
CSV_FILE="${CSV_FILE//\'/}"
CSV_FILE="${CSV_FILE//\"/}"

if [ ! -f "$CSV_FILE" ]; then
    echo -e "${RED}Error: File not found: ${CSV_FILE}${NC}"
    echo "Please check the path and try again."
    exit 1
fi

echo -e "${GREEN}Found: ${CSV_FILE}${NC}"
echo ""

# ---- Step 2: Sprint name ----
echo -e "${BLUE}Step 2: Which sprint are you analyzing?${NC}"
echo ""
echo "  Enter the sprint number or name (e.g., '27' or 'Sprint 27')"
echo ""
read -r -p "Sprint: " SPRINT_NAME
echo ""

# ---- Step 3: Team name ----
echo -e "${BLUE}Step 3: What is the team name?${NC}"
echo ""
echo "  This is used in the report header (e.g., 'Training Kubeflow')"
echo ""
read -r -p "Team name: " TEAM_NAME
echo ""

# ---- Step 4: Jira URL (optional) ----
echo -e "${BLUE}Step 4: Jira URL (optional - makes issue keys clickable in the report)${NC}"
echo ""
JIRA_URL="${JIRA_URL:-}"
if [ -z "$JIRA_URL" ]; then
    read -r -p "Jira URL (press Enter to skip): " JIRA_URL
fi
echo ""

# ---- Step 5: Output directory ----
OUTPUT_DIR="./reports"
echo -e "${BLUE}Step 5: Where should reports be saved?${NC}"
echo ""
read -r -p "Output directory [${OUTPUT_DIR}]: " USER_OUTPUT
if [ -n "$USER_OUTPUT" ]; then
    OUTPUT_DIR="$USER_OUTPUT"
fi
mkdir -p "$OUTPUT_DIR"
echo ""

# ---- Build the command ----
CMD="python3 \"${ANALYZER}\" --csv \"${CSV_FILE}\" --sprint \"${SPRINT_NAME}\" --team \"${TEAM_NAME}\" --output \"${OUTPUT_DIR}\""

if [ -n "$JIRA_URL" ]; then
    CMD="${CMD} --jira-url \"${JIRA_URL}\""
fi

# Check for history file (enables trend charts across sprints)
HISTORY_FILE="${OUTPUT_DIR}/${TEAM_NAME// /_}_history.json"
CMD="${CMD} --history \"${HISTORY_FILE}\""

echo -e "${BOLD}==========================================${NC}"
echo -e "${YELLOW}Generating report...${NC}"
echo ""

# Run the analyzer
eval "$CMD"

echo ""
echo -e "${GREEN}${BOLD}Done!${NC}"
echo ""
echo -e "Reports saved to: ${BOLD}${OUTPUT_DIR}/${NC}"
echo ""
echo "  Open the HTML report in your browser for the best experience."
echo "  The report includes:"
echo "    - Executive summary with KPI cards"
echo "    - 8-dimension health assessment"
echo "    - Anti-pattern detection"
echo "    - Coaching recommendations"
echo ""

# Try to open the HTML report
HTML_FILE=$(find "$OUTPUT_DIR" -name "*Health_Report.html" -newer "$CSV_FILE" 2>/dev/null | head -1)
if [ -n "$HTML_FILE" ]; then
    echo -e "  Report: ${BOLD}${HTML_FILE}${NC}"
    echo ""
    read -r -p "Open in browser? [Y/n]: " OPEN_BROWSER
    if [ "$OPEN_BROWSER" != "n" ] && [ "$OPEN_BROWSER" != "N" ]; then
        if command -v open &> /dev/null; then
            open "$HTML_FILE"
        elif command -v xdg-open &> /dev/null; then
            xdg-open "$HTML_FILE"
        elif command -v start &> /dev/null; then
            start "$HTML_FILE"
        else
            echo "  Could not auto-open. Please open the file manually."
        fi
    fi
fi
