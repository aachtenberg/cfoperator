#!/bin/bash
set -e

# Upload a CFOperator dashboard to Grafana Cloud
# Usage: ./upload-dashboard.sh [folder-name] [dashboard-file]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SECRETS_FILE="${REPO_DIR}/secrets/.env.secrets"
# Fallback to secrets directory in home
if [[ ! -f "$SECRETS_FILE" ]]; then
    SECRETS_FILE="$HOME/.config/cfoperator/.env.secrets"
fi
DASHBOARD_INPUT="${2:-cfoperator-dashboard.json}"
if [[ "$DASHBOARD_INPUT" = /* ]]; then
    DASHBOARD_FILE="$DASHBOARD_INPUT"
else
    DASHBOARD_FILE="$SCRIPT_DIR/$DASHBOARD_INPUT"
fi

# Load environment variables
if [[ ! -f "$SECRETS_FILE" ]]; then
    echo "❌ Secrets file not found at $SECRETS_FILE"
    echo "Expected: secrets/.env.secrets or ~/.config/cfoperator/.env.secrets"
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

REQUIRE_PG_DATASOURCE="false"
case "$(basename "$DASHBOARD_FILE")" in
    cfoperator-dashboard.json|event-runtime-dashboard.json)
        REQUIRE_PG_DATASOURCE="true"
        ;;
esac

if [[ "$REQUIRE_PG_DATASOURCE" == "true" ]]; then
    # Ensure PostgreSQL datasource for sweep reports exists
    SRE_PG_UID="${SRE_PG_DATASOURCE_UID:-ffcrf4dsqchz4e}"
    echo "🔌 Checking sre-knowledge PostgreSQL datasource (uid: $SRE_PG_UID)..."
    DS_CHECK=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $GRAFANA_CLOUD_API_KEY" \
        "$GRAFANA_CLOUD_URL/api/datasources/uid/$SRE_PG_UID")

    if [[ "$DS_CHECK" == "200" ]]; then
        echo "✓ Datasource exists"
    else
        echo "⚠️  sre-knowledge PostgreSQL datasource not found (uid: $SRE_PG_UID)"
        echo "   Create in Grafana UI: Connections → Add data source → PostgreSQL"
        echo "   Host: <your-db-host>:5434 | DB: sre_knowledge | User: sre_agent"
        echo "   Enable PDC proxy | SSL: disable"
        echo "   Then set SRE_PG_DATASOURCE_UID in .env.secrets to match the new UID"
    fi
else
    echo "ℹ️  Skipping PostgreSQL datasource check for $(basename "$DASHBOARD_FILE")"
fi

echo ""
echo "📊 Uploading dashboard to Grafana Cloud..."
echo "   Instance: $GRAFANA_CLOUD_URL"
echo "   Dashboard: $(basename "$DASHBOARD_FILE")"
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
        message: "CFOperator dashboard upload via script"
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
    echo "✅ Dashboard uploaded successfully!"
    echo "   UID: $DASH_UID"
    echo "   URL: $GRAFANA_CLOUD_URL$URL"
    echo ""
    echo "Dashboard file: $(basename "$DASHBOARD_FILE")"
    echo ""
else
    echo ""
    echo "❌ Failed to upload dashboard"
    echo "$RESPONSE" | jq '.'
    exit 1
fi
