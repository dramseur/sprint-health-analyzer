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

# ---- Step 1: Choose data source ----
echo -e "${BLUE}Step 1: How would you like to provide sprint data?${NC}"
echo ""
echo "  1) Jira CSV export file  (export from Jira board)"
echo "  2) Jira Sprint ID        (fetch directly from Jira API)"
echo "  3) MCP JSON file         (output from Jira MCP tools)"
echo ""
read -r -p "Choose [1/2/3]: " DATA_SOURCE
echo ""

INPUT_FLAG=""
SPRINT_NAME=""
NEED_SPRINT_NAME=true

case "$DATA_SOURCE" in
    2)
        # ---- Sprint ID mode ----
        echo -e "${BLUE}Enter the Jira Sprint ID${NC}"
        echo ""
        echo "  You can find this in the sprint URL or via the Jira API."
        echo ""
        read -r -p "Sprint ID: " SPRINT_ID
        echo ""

        if [ -z "$SPRINT_ID" ]; then
            echo -e "${RED}Error: Sprint ID is required.${NC}"
            exit 1
        fi

        INPUT_FLAG="--sprintid ${SPRINT_ID}"
        NEED_SPRINT_NAME=false

        # Check for Jira credentials
        if [ -z "$JIRA_URL" ] || [ -z "$JIRA_USER" ] || [ -z "$JIRA_TOKEN" ]; then
            echo -e "${YELLOW}Jira API credentials needed to fetch sprint data.${NC}"
            echo ""
            if [ -z "$JIRA_URL" ]; then
                read -r -p "Jira URL (e.g., https://your-org.atlassian.net): " JIRA_URL
            fi
            if [ -z "$JIRA_USER" ]; then
                read -r -p "Jira username/email: " JIRA_USER
            fi
            if [ -z "$JIRA_TOKEN" ]; then
                read -r -s -p "Jira API token: " JIRA_TOKEN
                echo ""
            fi
            echo ""

            if [ -z "$JIRA_URL" ] || [ -z "$JIRA_USER" ] || [ -z "$JIRA_TOKEN" ]; then
                echo -e "${RED}Error: All three credentials (URL, user, token) are required.${NC}"
                echo ""
                echo "  Set them as environment variables to skip this prompt:"
                echo "    export JIRA_URL=https://your-org.atlassian.net"
                echo "    export JIRA_USER=you@example.com"
                echo "    export JIRA_TOKEN=your-api-token"
                exit 1
            fi

            export JIRA_URL JIRA_USER JIRA_TOKEN
        fi
        echo -e "${GREEN}Will fetch sprint ${SPRINT_ID} from ${JIRA_URL}${NC}"
        echo ""
        ;;
    3)
        # ---- MCP JSON mode ----
        echo -e "${BLUE}Enter the path to the MCP JSON file${NC}"
        echo ""
        echo "  This is the raw JSON output from jira_get_sprint_issues or jira_search."
        echo ""
        read -r -p "Path to JSON file: " JSON_FILE
        echo ""

        # Handle drag-and-drop paths (remove quotes)
        JSON_FILE="${JSON_FILE//\'/}"
        JSON_FILE="${JSON_FILE//\"/}"

        if [ ! -f "$JSON_FILE" ]; then
            echo -e "${RED}Error: File not found: ${JSON_FILE}${NC}"
            exit 1
        fi

        INPUT_FLAG="--jira-json \"${JSON_FILE}\""
        echo -e "${GREEN}Found: ${JSON_FILE}${NC}"
        echo ""
        ;;
    *)
        # ---- CSV mode (default) ----
        echo -e "${BLUE}Enter the path to your Jira CSV export file${NC}"
        echo ""
        echo "  How to export from Jira:"
        echo "  1. Open your Jira board"
        echo "  2. Go to the Backlog or Active Sprint view"
        echo "  3. Click '...' menu > Export > CSV (All fields)"
        echo "  4. Save the file"
        echo ""
        read -r -p "Path to CSV file: " CSV_FILE
        echo ""

        # Handle drag-and-drop paths (remove quotes)
        CSV_FILE="${CSV_FILE//\'/}"
        CSV_FILE="${CSV_FILE//\"/}"

        if [ ! -f "$CSV_FILE" ]; then
            echo -e "${RED}Error: File not found: ${CSV_FILE}${NC}"
            exit 1
        fi

        INPUT_FLAG="--csv \"${CSV_FILE}\""
        echo -e "${GREEN}Found: ${CSV_FILE}${NC}"
        echo ""
        ;;
esac

# ---- Step 2: Sprint name (if not using sprint ID) ----
if [ "$NEED_SPRINT_NAME" = true ]; then
    echo -e "${BLUE}Step 2: Which sprint are you analyzing?${NC}"
    echo ""
    echo "  Enter the sprint number or name (e.g., '27' or 'Sprint 27')"
    echo ""
    read -r -p "Sprint: " SPRINT_NAME
    echo ""
fi

# ---- Step 3: Team name ----
STEP_NUM=3
if [ "$NEED_SPRINT_NAME" = false ]; then
    STEP_NUM=2
fi
echo -e "${BLUE}Step ${STEP_NUM}: What is the team name?${NC}"
echo ""
echo "  This is used in the report header (e.g., 'Training Kubeflow')"
echo ""
read -r -p "Team name: " TEAM_NAME
echo ""

# ---- Step 4: Jira URL (optional, if not already set) ----
STEP_NUM=$((STEP_NUM + 1))
if [ -z "$JIRA_URL" ]; then
    echo -e "${BLUE}Step ${STEP_NUM}: Jira URL (optional - makes issue keys clickable in the report)${NC}"
    echo ""
    read -r -p "Jira URL (press Enter to skip): " JIRA_URL
    echo ""
fi

# ---- Step 5: Output directory ----
STEP_NUM=$((STEP_NUM + 1))
OUTPUT_DIR="./reports"
echo -e "${BLUE}Step ${STEP_NUM}: Where should reports be saved?${NC}"
echo ""
read -r -p "Output directory [${OUTPUT_DIR}]: " USER_OUTPUT
if [ -n "$USER_OUTPUT" ]; then
    OUTPUT_DIR="$USER_OUTPUT"
fi
mkdir -p "$OUTPUT_DIR"
echo ""

# ---- Build the command ----
CMD="python3 \"${ANALYZER}\" ${INPUT_FLAG} --team \"${TEAM_NAME}\" --output \"${OUTPUT_DIR}\""

if [ -n "$SPRINT_NAME" ]; then
    CMD="${CMD} --sprint \"${SPRINT_NAME}\""
fi

if [ -n "$JIRA_URL" ]; then
    CMD="${CMD} --jira-url \"${JIRA_URL}\""
fi

# History file for trend charts across sprints
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
HTML_FILE=$(find "$OUTPUT_DIR" -name "*Health_Report.html" -maxdepth 1 -type f 2>/dev/null | sort -r | head -1)
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
