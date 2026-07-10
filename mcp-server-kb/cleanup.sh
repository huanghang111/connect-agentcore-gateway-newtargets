#!/bin/bash
# Tear down the Connect KB MCP Server.
#
# Deletes the CloudFormation stack (Runtime + Gateway + managed Knowledge Base)
# and the resources deploy.sh created outside the CLI: the DynamoDB pre-ticket
# table and the KB docs bucket.
#
# Production safety: this deletes the DynamoDB table INCLUDING stored pre-tickets.
# Requires confirmation unless run with --yes.

set -euo pipefail

cd "$(dirname "$0")"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
export AGENTCORE_SUPPRESS_RECOMMENDATION=1

# Load REGION from .env if present (must match what deploy.sh used).
if [ -f .env ]; then
    while IFS= read -r _line || [ -n "$_line" ]; do
        case "$_line" in ''|\#*) continue ;; esac
        _key="${_line%%=*}"; _val="${_line#*=}"
        _key="$(printf '%s' "$_key" | tr -d '[:space:]')"
        _val="${_val%\"}"; _val="${_val#\"}"; _val="${_val%\'}"; _val="${_val#\'}"
        if [ -z "$(eval "printf '%s' \"\${$_key:-}\"")" ]; then
            eval "export $_key=\"\$_val\""
        fi
    done < .env
fi

REGION="${REGION:-us-east-1}"
PROJECT="connectkb"
PRETICKET_TABLE="connectkb-pretickets"
STACK_NAME="AgentCore-${PROJECT}-default"

# The AgentCore CLI resolves region from AWS_REGION / AWS_DEFAULT_REGION.
export AWS_REGION="$REGION"
export AWS_DEFAULT_REGION="$REGION"

ASSUME_YES="no"; [ "${1:-}" = "--yes" ] && ASSUME_YES="yes"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
DOCS_BUCKET="${PROJECT}-kb-docs-${ACCOUNT_ID}-${REGION}"

echo -e "${YELLOW}=== Connect KB MCP Server Cleanup ===${NC}"
echo "  Will destroy the agentcore stack (Runtime / Gateway / managed KB),"
echo "  the DynamoDB table ${PRETICKET_TABLE} (⚠️ includes stored pre-tickets),"
echo "  and the docs bucket ${DOCS_BUCKET}."
echo ""
if [ "$ASSUME_YES" != "yes" ]; then
    read -r -p "Type 'delete' to proceed: " CONFIRM
    [ "$CONFIRM" = "delete" ] || { echo "Aborted."; exit 1; }
fi
echo ""

# `agentcore deploy` provisions a CloudFormation stack (Runtime + Gateway + KB);
# the CLI has no destroy command, so tear the stack down via CloudFormation.
# The AWS::BedrockAgentCore::Runtime delete can transiently time out
# (DELETE_FAILED / NotStabilized); a second delete-stack usually clears it, so
# retry once before giving up.
echo -e "${YELLOW}[1/3] Delete CloudFormation stack ${STACK_NAME}${NC}"
delete_stack_once() {
    aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
    echo "  waiting for stack deletion (Runtime / Gateway / KB)..."
    aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION" 2>/dev/null
}
if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
    delete_stack_once
    if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
        echo -e "${YELLOW}  first delete did not finish (likely Runtime delete timeout); retrying once...${NC}"
        delete_stack_once
    fi
    if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
        echo -e "${RED}  stack delete did not complete — check the CloudFormation console${NC}"
    else
        echo -e "${GREEN}✓ stack deleted${NC}"
    fi
else
    echo "  stack not found"
fi
echo ""

echo -e "${YELLOW}[2/3] Delete DynamoDB table${NC}"
if aws dynamodb describe-table --table-name "$PRETICKET_TABLE" --region "$REGION" >/dev/null 2>&1; then
    aws dynamodb delete-table --table-name "$PRETICKET_TABLE" --region "$REGION" >/dev/null
    echo -e "${GREEN}✓ deleted ${PRETICKET_TABLE}${NC}"
else
    echo "  not found"
fi
echo ""

echo -e "${YELLOW}[3/3] Delete KB docs bucket${NC}"
if aws s3 ls "s3://${DOCS_BUCKET}" --region "$REGION" >/dev/null 2>&1; then
    aws s3 rb "s3://${DOCS_BUCKET}" --force --region "$REGION" >/dev/null
    echo -e "${GREEN}✓ deleted ${DOCS_BUCKET}${NC}"
else
    echo "  not found"
fi
echo ""
echo -e "${GREEN}=== Cleanup Complete ===${NC}"
