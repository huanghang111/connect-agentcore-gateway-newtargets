#!/bin/bash
# One-click deploy for the Connect KB MCP Server.
#
# Wraps the AgentCore CLI (@aws/agentcore). The CLI owns Runtime + Gateway + the
# managed Knowledge Base (Container build → CodeBuild ARM64, no local Docker).
# This wrapper handles the few things the CLI does not:
#   1. DynamoDB pre-ticket table (createPreTicket / getPreTicket)
#   2. KB docs S3 bucket + sample-docs upload, wired into agentcore.json
#   3. Gateway inbound auth (NONE by default, CUSTOM_JWT if OIDC_DISCOVERY_URL set)
#   4. agentcore deploy
#   5. KB data-source ingestion (so the sample docs are searchable)
#
# Config via env vars (all optional):
#   REGION               AWS region (default us-east-1)
#   OIDC_DISCOVERY_URL   Connect OIDC discovery URL. Empty = Gateway uses NONE auth.
#
# Prereqs: node/npm (CloudShell has them), awscli, and `npm i -g @aws/agentcore`.

set -euo pipefail

cd "$(dirname "$0")"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
export AGENTCORE_SUPPRESS_RECOMMENDATION=1

# Load config from .env if present (REGION / OIDC_DISCOVERY_URL / OIDC_ALLOWED_CLIENTS).
# Inline env vars still win: `source` here sets them, but a var already exported on
# the command line (e.g. `REGION=us-west-2 ./deploy.sh`) was set BEFORE this runs,
# and `.env` uses plain `KEY=value` — so to let the CLI override, we only apply
# .env values that aren't already set. This mirrors mcp-agent/'s convention.
if [ -f .env ]; then
    while IFS= read -r _line || [ -n "$_line" ]; do
        case "$_line" in ''|\#*) continue ;; esac
        _key="${_line%%=*}"; _val="${_line#*=}"
        _key="$(printf '%s' "$_key" | tr -d '[:space:]')"
        # strip surrounding quotes from value
        _val="${_val%\"}"; _val="${_val#\"}"; _val="${_val%\'}"; _val="${_val#\'}"
        # only set if not already provided in the environment
        if [ -z "$(eval "printf '%s' \"\${$_key:-}\"")" ]; then
            eval "export $_key=\"\$_val\""
        fi
    done < .env
fi

REGION="${REGION:-us-east-1}"
# OIDC_DISCOVERY_URL empty  → Gateway inbound auth = NONE (PoC).
# OIDC_DISCOVERY_URL set    → CUSTOM_JWT; deploy.sh does a two-pass deploy to set
#                             allowedAudience to the created Gateway's own ID
#                             (Connect's JWT `aud` claim == the Gateway ID).
# OIDC_ALLOWED_CLIENTS      → optional comma-separated allowedClients for CUSTOM_JWT.
OIDC_DISCOVERY_URL="${OIDC_DISCOVERY_URL:-}"
OIDC_ALLOWED_CLIENTS="${OIDC_ALLOWED_CLIENTS:-}"
export OIDC_ALLOWED_CLIENTS

# The AgentCore CLI resolves region from the AWS SDK env (AWS_REGION /
# AWS_DEFAULT_REGION) or the profile — `agentcore deploy` has no --region flag.
# Export both so the CDK deploy, CodeBuild, and KB all land in $REGION.
export AWS_REGION="$REGION"
export AWS_DEFAULT_REGION="$REGION"

# Names — must match app/kbserver env vars + kb-ddb-policy.json.
PROJECT="connectkb"
KB_NAME="connectkb-kb"
GATEWAY_NAME="connectkb-gw"
PRETICKET_TABLE="connectkb-pretickets"

command -v agentcore >/dev/null 2>&1 || { echo -e "${RED}✗ agentcore CLI not found. Run: npm install -g @aws/agentcore${NC}"; exit 1; }
command -v aws >/dev/null 2>&1 || { echo -e "${RED}✗ awscli not found.${NC}"; exit 1; }

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
DOCS_BUCKET="${PROJECT}-kb-docs-${ACCOUNT_ID}-${REGION}"

echo -e "${YELLOW}=== Connect KB MCP Server Deploy ===${NC}"
echo "  Account: ${ACCOUNT_ID}   Region: ${REGION}"
echo "  Docs bucket: ${DOCS_BUCKET}"
echo "  Gateway auth: $([ -n "$OIDC_DISCOVERY_URL" ] && echo CUSTOM_JWT || echo 'NONE (PoC only)')"
echo ""

# The AgentCore CLI validates the deployment target's account against STS and
# fails if they differ. The committed aws-targets.json must therefore be written
# from the *current* credentials + region, not carry a baked-in account — this is
# what makes a fresh clone deploy into any account one-shot.
ACCOUNT_ID="$ACCOUNT_ID" REGION="$REGION" python3 - <<'PY'
import json, os
json.dump(
    [{"name": "default", "account": os.environ["ACCOUNT_ID"], "region": os.environ["REGION"]}],
    open("agentcore/aws-targets.json", "w"), indent=2,
)
open("agentcore/aws-targets.json", "a").write("\n")
PY
echo -e "${GREEN}✓ aws-targets.json set to ${ACCOUNT_ID}/${REGION}${NC}"
echo ""

# ---------------------------------------------------------------- 1. DynamoDB
echo -e "${YELLOW}[1/5] DynamoDB pre-ticket table${NC}"
if aws dynamodb describe-table --table-name "$PRETICKET_TABLE" --region "$REGION" >/dev/null 2>&1; then
    echo -e "${GREEN}✓ exists${NC}"
else
    aws dynamodb create-table \
        --table-name "$PRETICKET_TABLE" --region "$REGION" \
        --attribute-definitions AttributeName=ticketId,AttributeType=S \
        --key-schema AttributeName=ticketId,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST >/dev/null
    aws dynamodb wait table-exists --table-name "$PRETICKET_TABLE" --region "$REGION"
    echo -e "${GREEN}✓ created${NC}"
fi
echo ""

# ---------------------------------------------------- 2. KB docs bucket + upload
echo -e "${YELLOW}[2/5] KB docs S3 bucket + sample docs${NC}"
if ! aws s3 ls "s3://${DOCS_BUCKET}" --region "$REGION" >/dev/null 2>&1; then
    if [ "$REGION" = "us-east-1" ]; then
        aws s3 mb "s3://${DOCS_BUCKET}" --region "$REGION" >/dev/null
    else
        aws s3api create-bucket --bucket "$DOCS_BUCKET" --region "$REGION" \
            --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
    fi
fi
aws s3 sync app/kbserver/sample-docs "s3://${DOCS_BUCKET}/" --region "$REGION" --delete >/dev/null
echo -e "${GREEN}✓ uploaded sample docs${NC}"
echo ""

# ------------------------------------------- 3. Patch agentcore.json (bucket+auth)
echo -e "${YELLOW}[3/5] Wire KB data source + gateway auth into agentcore.json${NC}"
DOCS_BUCKET="$DOCS_BUCKET" GATEWAY_NAME="$GATEWAY_NAME" KB_NAME="$KB_NAME" \
OIDC_DISCOVERY_URL="$OIDC_DISCOVERY_URL" python3 - <<'PY'
import json, os
p = "agentcore/agentcore.json"
d = json.load(open(p))
bucket = os.environ["DOCS_BUCKET"]
for kb in d.get("knowledgeBases", []):
    if kb.get("name") == os.environ["KB_NAME"]:
        kb["dataSources"] = [{"type": "S3", "uri": f"s3://{bucket}"}]
url = os.environ.get("OIDC_DISCOVERY_URL", "").strip()
clients = [c.strip() for c in os.environ.get("OIDC_ALLOWED_CLIENTS", "").split(",") if c.strip()]
for gw in d.get("agentCoreGateways", []):
    if gw.get("name") != os.environ["GATEWAY_NAME"]:
        continue
    if url:
        gw["authorizerType"] = "CUSTOM_JWT"
        # Connect issues JWTs whose `aud` == the Gateway's own ID, which is only
        # known after the Gateway is created (random suffix). CUSTOM_JWT requires
        # at least one of allowedAudience/allowedClients, so we seed a placeholder
        # audience here and rewrite it to the real Gateway ID in pass 2 (step 6).
        # Preserve an already-resolved audience across redeploys so we don't churn
        # back to the placeholder once it's correct.
        existing = (gw.get("authorizerConfiguration", {})
                      .get("customJwtAuthorizer", {}).get("allowedAudience", []))
        aud = [a for a in existing if a and a != "__pending__"] or ["__pending__"]
        cfg = {"discoveryUrl": url, "allowedAudience": aud}
        if clients:
            cfg["allowedClients"] = clients
        gw["authorizerConfiguration"] = {"customJwtAuthorizer": cfg}
    else:
        gw["authorizerType"] = "NONE"
        gw.pop("authorizerConfiguration", None)
json.dump(d, open(p, "w"), indent=2, ensure_ascii=False)
print("  ✓ agentcore.json patched")
PY
agentcore validate >/dev/null && echo -e "${GREEN}✓ config valid${NC}"
echo ""

# ------- 3b. Guard: authorizer type is immutable on an existing Gateway --------
# AgentCore rejects changing a Gateway's authorizerType in place ("Authorizer
# type cannot be updated for an existing gateway"). If a stack already exists
# with a different inbound-auth mode than we're about to deploy, the Gateway
# must be REPLACED, which means deleting the stack first. Detect and stop with
# clear guidance rather than failing mid-deploy with a rollback.
WANT_AUTH="$([ -n "$OIDC_DISCOVERY_URL" ] && echo CUSTOM_JWT || echo NONE)"
if aws cloudformation describe-stacks --stack-name "AgentCore-${PROJECT}-default" --region "$REGION" >/dev/null 2>&1; then
    LIVE_AUTH="$(python3 -c "
import boto3
c=boto3.client('bedrock-agentcore-control',region_name='${REGION}')
for p in c.get_paginator('list_gateways').paginate():
    for g in p.get('items',[]):
        if g['name']=='${PROJECT}-${GATEWAY_NAME}':
            print(c.get_gateway(gatewayIdentifier=g['gatewayId']).get('authorizerType','')); break
" 2>/dev/null || echo "")"
    if [ -n "$LIVE_AUTH" ] && [ "$LIVE_AUTH" != "$WANT_AUTH" ]; then
        echo -e "${RED}✗ Existing Gateway uses inbound auth '${LIVE_AUTH}', but this run wants '${WANT_AUTH}'.${NC}"
        echo -e "${RED}  AgentCore cannot change a Gateway's authorizer type in place — the Gateway must be recreated.${NC}"
        echo -e "${YELLOW}  Run  ./cleanup.sh --yes  first, then re-run this deploy.${NC}"
        exit 1
    fi
fi

# ------------------------------------------------------------------ 4. Deploy
echo -e "${YELLOW}[4/5] agentcore deploy (Runtime + Gateway + KB via CDK/CodeBuild)${NC}"
# The CDK project needs its node deps (tsc etc.) present before `agentcore deploy`
# runs "Build CDK project". `agentcore create` installs them, but a fresh clone
# does not carry node_modules, so install them here (idempotent).
if [ ! -d agentcore/cdk/node_modules ]; then
    echo "  Installing CDK project dependencies (npm ci)..."
    ( cd agentcore/cdk && { npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund; } ) >/dev/null
    echo -e "${GREEN}  ✓ CDK deps installed${NC}"
fi
# `agentcore deploy` is a long CDK run (CodeBuild + many CFN/AgentCore API calls
# over several minutes); a transient network blip (EADDRNOTAVAIL / socket timeout
# / ENOTFOUND) can abort it mid-flight. The deploy is idempotent, so retry a few
# times before giving up.
deploy_with_retry() {
    local attempt=1 max=3
    while true; do
        if agentcore deploy --yes; then
            return 0
        fi
        if [ "$attempt" -ge "$max" ]; then
            echo -e "${RED}✗ agentcore deploy failed after ${max} attempts.${NC}"
            return 1
        fi
        echo -e "${YELLOW}  deploy attempt ${attempt} failed (often a transient network error); retrying in 15s...${NC}"
        attempt=$((attempt + 1)); sleep 15
    done
}
deploy_with_retry
echo -e "${GREEN}✓ deployed${NC}"
echo ""

# ------------------------------- 4b. CUSTOM_JWT: rewrite audience to Gateway ID
# Connect's JWT `aud` claim equals the Gateway's own ID. That ID only exists
# after the first deploy, so if we used a placeholder audience, read the real
# Gateway ID from deployed-state and redeploy once so the authorizer accepts
# Connect's tokens. Idempotent: a second run sees the real audience already set
# and the pass-1 patch preserves it, so this block no-ops.
if [ -n "$OIDC_DISCOVERY_URL" ]; then
    GW_ID="$(python3 -c "
import json
try:
    d = json.load(open('agentcore/.cli/deployed-state.json'))
    print(d['targets']['default']['resources']['gateways']['${GATEWAY_NAME}']['gatewayId'])
except Exception:
    print('')
")"
    CUR_AUD="$(python3 -c "
import json
d = json.load(open('agentcore/agentcore.json'))
for g in d['agentCoreGateways']:
    if g['name']=='${GATEWAY_NAME}':
        print(','.join(g.get('authorizerConfiguration',{}).get('customJwtAuthorizer',{}).get('allowedAudience',[])))
")"
    if [ -n "$GW_ID" ] && [ "$CUR_AUD" != "$GW_ID" ]; then
        echo -e "${YELLOW}[4b] Set CUSTOM_JWT allowedAudience = Gateway ID (${GW_ID}) and redeploy${NC}"
        GW_ID="$GW_ID" GATEWAY_NAME="$GATEWAY_NAME" python3 - <<'PY'
import json, os
p = "agentcore/agentcore.json"; d = json.load(open(p))
for g in d["agentCoreGateways"]:
    if g["name"] == os.environ["GATEWAY_NAME"]:
        jwt = g["authorizerConfiguration"]["customJwtAuthorizer"]
        jwt["allowedAudience"] = [os.environ["GW_ID"]]
json.dump(d, open(p, "w"), indent=2, ensure_ascii=False)
print("  ✓ audience set")
PY
        agentcore deploy --yes
        echo -e "${GREEN}✓ audience applied${NC}"
        echo ""
    fi
fi

# --------------------------------------------------------------- 5. Status
# `agentcore deploy` auto-ingests the KB (step "Auto-ingest knowledge bases"),
# and step 2 already synced the docs BEFORE deploy, so there is nothing to
# ingest separately. Just surface the deployed state.
echo -e "${YELLOW}[5/5] Status${NC}"
agentcore status || true
echo ""
echo -e "${GREEN}=== Done ===${NC}"
echo "Verify a tool call:"
echo "  cd $(pwd) && agentcore invoke --gateway-target-name kbserver-target \\"
echo "    --tool searchKnowledgeBase --input '{\"query\":\"保修期多久\"}' call-tool"
echo "Then fetch the Gateway MCP endpoint (agentcore status) and wire it into Connect."
