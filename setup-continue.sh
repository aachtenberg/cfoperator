#!/bin/bash
# Setup Continue integration with CFOperator MCP server

set -e

echo "=================================="
echo "Continue + CFOperator Setup"
echo "=================================="
echo ""

# Check if running on the right host
if [ "$(hostname)" != "raspberrypi3" ]; then
    echo "⚠️  Warning: This script is designed for raspberrypi3"
    echo "   Current host: $(hostname)"
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Step 1: Check Docker
echo "Step 1: Checking Docker setup..."
if ! docker compose ps cfoperator &>/dev/null; then
    echo "❌ CFOperator container not running"
    echo "   Run: docker compose up -d"
    exit 1
fi
echo "✅ CFOperator container is running"

# Step 2: Rebuild with MCP support
echo ""
echo "Step 2: Rebuilding container with MCP support..."
read -p "Rebuild CFOperator container? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    docker compose up --build -d
    echo "✅ Container rebuilt"
else
    echo "⏭️  Skipping rebuild"
fi

# Step 3: Test MCP server
echo ""
echo "Step 3: Testing MCP server..."
if docker exec -it cfoperator python /app/test_mcp.py; then
    echo "✅ MCP server works!"
else
    echo "❌ MCP server test failed"
    echo "   Check logs: docker compose logs cfoperator"
    exit 1
fi

# Step 4: Check Continue installation
echo ""
echo "Step 4: Checking Continue CLI..."
if command -v cn &> /dev/null; then
    echo "✅ Continue CLI installed: $(which cn)"
    CN_CMD="cn"
elif command -v npx &> /dev/null; then
    echo "⚠️  Continue CLI not found, will use npx"
    echo "   Creating alias: cn='npx @continuedev/cli'"
    CN_CMD="npx @continuedev/cli"

    # Add alias to bashrc if not already there
    if ! grep -q "alias cn=" ~/.bashrc 2>/dev/null; then
        echo "alias cn='npx @continuedev/cli'" >> ~/.bashrc
        echo "   Added alias to ~/.bashrc (restart shell to use)"
    fi
else
    echo "❌ Neither Continue CLI nor npx found"
    echo "   Install Continue: npm install -g @continuedev/cli"
    echo "   Or install Node.js to use npx"
    exit 1
fi

# Step 5: Configure Continue
echo ""
echo "Step 5: Configuring Continue..."

CONFIG_FILE="$HOME/.continue/config.json"
mkdir -p "$HOME/.continue"

if [ -f "$CONFIG_FILE" ]; then
    echo "⚠️  Config file exists: $CONFIG_FILE"
    read -p "Backup and overwrite? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        cp "$CONFIG_FILE" "$CONFIG_FILE.backup.$(date +%s)"
        echo "   Backed up to $CONFIG_FILE.backup.*"
    else
        echo "⏭️  Skipping config update"
        echo "   Manually add this to $CONFIG_FILE:"
        echo '   "mcpServers": {'
        echo '     "cfoperator": {'
        echo '       "command": "docker",'
        echo '       "args": ["exec", "-i", "cfoperator", "python", "/app/mcp_server.py"]'
        echo '     }'
        echo '   }'
        CONFIG_FILE=""
    fi
fi

if [ -n "$CONFIG_FILE" ]; then
    # Create or update config
    cat > "$CONFIG_FILE" << 'EOF'
{
  "models": [
    {
      "title": "Ollama Qwen",
      "provider": "ollama",
      "model": "qwen2.5-coder:14b",
      "apiBase": "http://192.168.0.150:11434"
    }
  ],
  "mcpServers": {
    "cfoperator": {
      "command": "docker",
      "args": ["exec", "-i", "cfoperator", "python", "/app/mcp_server.py"]
    }
  }
}
EOF
    echo "✅ Config written to $CONFIG_FILE"
fi

# Step 6: Test integration
echo ""
echo "Step 6: Testing integration..."
echo "Running: $CN_CMD \"@cfoperator list containers\""
echo ""

if $CN_CMD "@cfoperator list containers" 2>&1 | head -20; then
    echo ""
    echo "✅ Integration test passed!"
else
    echo ""
    echo "⚠️  Test completed but check output above for errors"
fi

# Done!
echo ""
echo "=================================="
echo "Setup Complete! 🎉"
echo "=================================="
echo ""
echo "Try these commands:"
echo "  $CN_CMD \"@cfoperator list containers\""
echo "  $CN_CMD \"@cfoperator compare all hosts\""
echo "  $CN_CMD \"@cfoperator why did prometheus restart?\""
echo ""
echo "Documentation:"
echo "  - Continue integration: docs/continue-integration.md"
echo "  - MCP details: MCP_INTEGRATION.md"
echo "  - Available tools: $CN_CMD \"what can @cfoperator do?\""
echo ""
