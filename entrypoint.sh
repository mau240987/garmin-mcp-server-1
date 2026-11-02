#!/bin/bash
set -e

echo "============================================"
echo "  Garmin MCP Server"
echo "============================================"
echo ""

# Check if Garmin tokens exist (= already logged in before)
if [ -d "/data/tokens" ] && [ "$(ls -A /data/tokens 2>/dev/null)" ]; then
    echo "  Found saved Garmin tokens."
    echo "  Credentials are NOT required."
    echo ""
else
    echo "  First run — no saved tokens found."
    echo ""
    if [ -z "$GARMIN_EMAIL" ] || [ -z "$GARMIN_PASSWORD" ]; then
        echo "  ERROR: GARMIN_EMAIL and GARMIN_PASSWORD are required on first run."
        echo ""
        echo "  Run with:"
        echo ""
        echo "    docker run -d \\"
        echo "      -p 8000:8000 \\"
        echo "      -e GARMIN_EMAIL=your@email.com \\"
        echo "      -e GARMIN_PASSWORD=yourpassword \\"
        echo "      -v garmin-tokens:/data/tokens \\"
        echo "      garmin-mcp-server"
        echo ""
        echo "  After the first successful login, tokens are saved in the"
        echo "  'garmin-tokens' volume. You can then restart WITHOUT credentials:"
        echo ""
        echo "    docker run -d \\"
        echo "      -p 8000:8000 \\"
        echo "      -v garmin-tokens:/data/tokens \\"
        echo "      garmin-mcp-server"
        echo ""
        exit 1
    fi
    echo "  Credentials provided. Will authenticate on first API call."
    echo ""
fi

echo "  Transport:  HTTP (streamable-http)"
echo "  Endpoint:   http://localhost:${PORT:-8000}/mcp"
echo ""
echo "  Connect from Claude Desktop:"
echo "    Add to claude_desktop_config.json:"
echo ""
echo "    \"garmin\": {"
echo "      \"command\": \"npx\","
echo "      \"args\": [\"-y\", \"mcp-remote\", \"http://localhost:${PORT:-8000}/mcp\"]"
echo "    }"
echo ""
echo "============================================"
echo ""

exec garmin-coach-mcp \
    --transport http \
    --host 0.0.0.0 \
    --port "${PORT:-8000}"
