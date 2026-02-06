#!/bin/bash
set -e

# Upload CFOperator dashboard to Grafana Cloud
# Usage: ./upload-dashboard.sh [folder-name]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
SECRETS_FILE="$HOME/repos/homelab-infra/secrets/.env.secrets"
DASHBOARD_FILE="$SCRIPT_DIR/cfoperator-dashboard.json"

# Load environment variables
if [[ ! -f "$SECRETS_FILE" ]]; then
    echo "❌ Secrets file not found at $SECRETS_FILE"
    echo "Expected: ~/repos/homelab-infra/secrets/.env.secrets"
    exit 1
fi

source "$SECRETS_FILE"

if [[ -z "$GRAFANA_CLOUD_URL" ]] || [[ -z "$GRAFANA_CLOUD_API_KEY" ]]; then
    echo "❌ Missing Grafana Cloud credentials in secrets/.env.secrets"
    echo "Required: GRAFANA_CLOUD_URL and GRAFANA_CLOUD_API_KEY"
    exit 1
fi

if [[ ! -f "$DASHBOARD_FILE" ]]; then
    echo "❌ Dashboard file not found: $DASHBOARD_FILE"
    exit 1
fi

FOLDER_NAME="${1:-CFOperator}"

echo "📊 Uploading CFOperator dashboard to Grafana Cloud..."
echo "   Instance: $GRAFANA_CLOUD_URL"
echo "   Dashboard: cfoperator-dashboard.json"
echo "   Folder: $FOLDER_NAME"
echo ""

# Find or create folder
FOLDER_ID=""
FOLDER_UID=""
if [[ "$FOLDER_NAME" != "General" ]]; then
    echo "🔍 Looking for folder: $FOLDER_NAME"
    FOLDERS_JSON=$(curl -s -H "Authorization: Bearer $GRAFANA_CLOUD_API_KEY" "$GRAFANA_CLOUD_URL/api/folders")

    # Check if response is an array
    if echo "$FOLDERS_JSON" | jq -e 'type == "array"' > /dev/null 2>&1; then
        FOLDER_UID=$(echo "$FOLDERS_JSON" | jq -r ".[] | select(.title==\"$FOLDER_NAME\") | .uid")
    fi

    if [[ -z "$FOLDER_UID" ]] || [[ "$FOLDER_UID" == "null" ]]; then
        echo "📁 Creating folder: $FOLDER_NAME"
        FOLDER_CREATE=$(curl -s -X POST -H "Authorization: Bearer $GRAFANA_CLOUD_API_KEY" \
            -H "Content-Type: application/json" \
            -d "{\"title\":\"$FOLDER_NAME\"}" \
            "$GRAFANA_CLOUD_URL/api/folders")

        FOLDER_UID=$(echo "$FOLDER_CREATE" | jq -r '.uid')

        if [[ -z "$FOLDER_UID" ]] || [[ "$FOLDER_UID" == "null" ]]; then
            echo "❌ Failed to create folder"
            echo "$FOLDER_CREATE" | jq '.'
            exit 1
        fi
    fi

    echo "✓ Folder UID: $FOLDER_UID"
fi

# Read dashboard JSON - handle both wrapped and unwrapped formats
if jq -e '.dashboard' "$DASHBOARD_FILE" > /dev/null 2>&1; then
    # Wrapped format: { "dashboard": {...} }
    DASHBOARD_JSON=$(jq '.dashboard' "$DASHBOARD_FILE")
else
    # Unwrapped format: dashboard object directly
    DASHBOARD_JSON=$(jq '.' "$DASHBOARD_FILE")
fi

# Wrap dashboard JSON in API format
API_PAYLOAD=$(jq -n \
    --argjson dashboard "$DASHBOARD_JSON" \
    --arg folderUid "$FOLDER_UID" \
    '{
        dashboard: ($dashboard | .id = null),
        folderUid: (if $folderUid != "" then $folderUid else null end),
        overwrite: true,
        message: "CFOperator v1.0.2 - Fleet-wide monitoring + LLM observability"
    }')

# Create/update dashboard
echo "🚀 Pushing dashboard to Grafana Cloud..."
RESPONSE=$(curl -s -X POST -H "Authorization: Bearer $GRAFANA_CLOUD_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$API_PAYLOAD" \
    "$GRAFANA_CLOUD_URL/api/dashboards/db")

# Check response
STATUS=$(echo "$RESPONSE" | jq -r '.status // empty')
URL=$(echo "$RESPONSE" | jq -r '.url // empty')
DASH_UID=$(echo "$RESPONSE" | jq -r '.uid // empty')

if [[ "$STATUS" == "success" ]] && [[ -n "$URL" ]]; then
    echo ""
    echo "✅ CFOperator dashboard uploaded successfully!"
    echo "   UID: $DASH_UID"
    echo "   URL: $GRAFANA_CLOUD_URL$URL"
    echo ""
    echo "Dashboard includes:"
    echo "   • Key metrics (uptime, hosts, containers, error rate)"
    echo "   • LLM observability (requests, tokens, latency, fallbacks)"
    echo "   • OODA loop activity and tool usage"
    echo "   • Infrastructure health (CPU, memory by host)"
    echo "   • Comprehensive log panels (7 specialized views)"
    echo ""
else
    echo ""
    echo "❌ Failed to upload dashboard"
    echo "$RESPONSE" | jq '.'
    exit 1
fi
