#!/bin/bash
# Cleanup MCP Agent resources (reverses deploy.sh)

set -e

cd "$(dirname "$0")"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

if [ ! -f ".env" ]; then
    echo -e "${RED}✗ .env not found. Copy .env.example to .env and fill in your values.${NC}"
    exit 1
fi
set -a
# shellcheck disable=SC1091
source .env
set +a

: "${REGION:?REGION is required in .env}"
: "${AGENT_NAME:?AGENT_NAME is required in .env}"
: "${ECR_REPO_NAME:?ECR_REPO_NAME is required in .env}"
: "${TARGET_NAME:?TARGET_NAME is required in .env}"

if [ -z "${ACCOUNT_ID:-}" ]; then
    ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
fi
if [ -z "${ACCOUNT_ID}" ]; then
    echo -e "${RED}✗ ACCOUNT_ID is empty and STS lookup failed.${NC}"
    exit 1
fi

# Gateway is optional in .env. If GATEWAY_ID is empty we treat the Gateway as
# auto-created by deploy.sh and look it up by the conventional name.
GATEWAY_ID="${GATEWAY_ID:-}"
GATEWAY_SERVICE_ROLE="${GATEWAY_SERVICE_ROLE:-}"
AUTO_GATEWAY_NAME="${AGENT_NAME}-gw"
AUTO_GATEWAY_ROLE="${AGENT_NAME}-gateway-role"

RUNTIME_NAME="${AGENT_NAME//-/_}"
CODEBUILD_PROJECT_NAME="${AGENT_NAME}-build"
CODEBUILD_ROLE_NAME="${AGENT_NAME}-codebuild-role"
S3_BUCKET="${AGENT_NAME}-source-${ACCOUNT_ID}"
EXECUTION_ROLE_NAME="${AGENT_NAME}-execution-role"

echo -e "${YELLOW}=== MCP Agent Cleanup ===${NC}\n"

# Resolve a Python interpreter with a recent-enough boto3 (must expose
# iamCredentialProvider in the AgentCore Gateway Target schema).
check_schema() {
    "$1" - <<'PY' >/dev/null 2>&1
import sys, boto3
try:
    op = boto3.client("bedrock-agentcore-control", region_name="us-east-1") \
        .meta.service_model.operation_model("CreateGatewayTarget")
    fields = op.input_shape.members["credentialProviderConfigurations"] \
        .member.members["credentialProvider"].members
    sys.exit(0 if "iamCredentialProvider" in fields else 1)
except Exception:
    sys.exit(1)
PY
}

if python3 -c 'import boto3' >/dev/null 2>&1 && check_schema "$(command -v python3)"; then
    PY="$(command -v python3)"
else
    if [ ! -x ./.venv/bin/python ]; then
        echo "  Creating .venv ..."
        python3 -m venv .venv
    fi
    if ! check_schema ./.venv/bin/python; then
        echo "  Installing/upgrading boto3 into .venv ..."
        ./.venv/bin/pip install --upgrade pip 'boto3>=1.43.0' 'botocore>=1.43.0' -q
    fi
    if ! check_schema ./.venv/bin/python; then
        echo -e "${RED}✗ Even after upgrade, boto3 schema is missing iamCredentialProvider${NC}"
        exit 1
    fi
    PY="./.venv/bin/python"
fi

# Resolve Gateway ID + role: either user-provided, or auto-created by deploy.sh.
RESOLVED_GATEWAY_ID=$("$PY" -c "
import boto3
c = boto3.client('bedrock-agentcore-control', region_name='${REGION}')
explicit = '${GATEWAY_ID}'.strip()
if explicit:
    print(explicit)
else:
    for page in c.get_paginator('list_gateways').paginate():
        for gw in page.get('items', []):
            if gw.get('name') == '${AUTO_GATEWAY_NAME}':
                print(gw['gatewayId'])
                break
        else:
            continue
        break
" 2>/dev/null || echo "")

GATEWAY_AUTO_CREATED="no"
if [ -z "${GATEWAY_ID}" ] && [ -n "$RESOLVED_GATEWAY_ID" ]; then
    GATEWAY_AUTO_CREATED="yes"
fi

RESOLVED_GATEWAY_ROLE="${GATEWAY_SERVICE_ROLE}"
if [ -z "$RESOLVED_GATEWAY_ROLE" ]; then
    RESOLVED_GATEWAY_ROLE="${AUTO_GATEWAY_ROLE}"
fi

# Step 1: Remove Gateway Target
echo -e "${YELLOW}Step 1: Remove Gateway Target${NC}"
if [ -n "$RESOLVED_GATEWAY_ID" ]; then
    TARGET_ID=$("$PY" -c "
import boto3
c = boto3.client('bedrock-agentcore-control', region_name='${REGION}')
resp = c.list_gateway_targets(gatewayIdentifier='${RESOLVED_GATEWAY_ID}')
for t in resp.get('items', []):
    if t.get('name') == '${TARGET_NAME}':
        print(t['targetId'])
        break
" 2>/dev/null || echo "")
    if [ -n "$TARGET_ID" ]; then
        "$PY" -c "
import boto3
c = boto3.client('bedrock-agentcore-control', region_name='${REGION}')
c.delete_gateway_target(gatewayIdentifier='${RESOLVED_GATEWAY_ID}', targetId='${TARGET_ID}')
print('  ✓ Gateway target deleted: ${TARGET_ID}')
" 2>/dev/null || echo "  Failed to delete target"
    else
        echo "  No target found"
    fi
else
    echo "  No Gateway resolved, skipping"
fi
echo ""

# Step 2: Remove Gateway IAM inline policy (only meaningful if role exists)
echo -e "${YELLOW}Step 2: Remove Gateway IAM Policy${NC}"
if aws iam get-role --role-name "${RESOLVED_GATEWAY_ROLE}" >/dev/null 2>&1; then
    aws iam delete-role-policy \
        --role-name "${RESOLVED_GATEWAY_ROLE}" \
        --policy-name InvokeAgentRuntimePolicy 2>/dev/null && \
        echo -e "${GREEN}✓ Gateway IAM policy removed${NC}" || \
        echo "  No InvokeAgentRuntimePolicy found"
else
    echo "  Gateway role not found, skipping"
fi
echo ""

# Step 3: Delete AgentCore Runtime
echo -e "${YELLOW}Step 3: Delete AgentCore Runtime${NC}"
RUNTIME_ID=$("$PY" -c "
import boto3
client = boto3.client('bedrock-agentcore-control', region_name='${REGION}')
resp = client.list_agent_runtimes()
for rt in resp.get('agentRuntimes', []):
    if rt['agentRuntimeName'] == '${RUNTIME_NAME}':
        print(rt['agentRuntimeId'])
        break
" 2>/dev/null || echo "")

if [ -n "$RUNTIME_ID" ]; then
    "$PY" -c "
import boto3
client = boto3.client('bedrock-agentcore-control', region_name='${REGION}')
client.delete_agent_runtime(agentRuntimeId='${RUNTIME_ID}')
print('  ✓ Runtime deleted: ${RUNTIME_ID}')
" 2>/dev/null || echo "  Failed to delete runtime"
else
    echo "  No runtime found"
fi
echo ""

# Step 4: Delete CodeBuild Project
echo -e "${YELLOW}Step 4: Delete CodeBuild Project${NC}"
if aws codebuild batch-get-projects --names ${CODEBUILD_PROJECT_NAME} --region ${REGION} \
    --query "projects[0].name" --output text 2>/dev/null | grep -q "${CODEBUILD_PROJECT_NAME}"; then
    aws codebuild delete-project --name ${CODEBUILD_PROJECT_NAME} --region ${REGION}
    echo -e "${GREEN}✓ CodeBuild project deleted${NC}"
else
    echo "  No CodeBuild project found"
fi
echo ""

# Step 5: Delete S3 Bucket
echo -e "${YELLOW}Step 5: Delete S3 Bucket${NC}"
if aws s3 ls s3://${S3_BUCKET} --region ${REGION} 2>/dev/null >/dev/null; then
    aws s3 rb s3://${S3_BUCKET} --force --region ${REGION}
    echo -e "${GREEN}✓ S3 bucket deleted${NC}"
else
    echo "  No S3 bucket found"
fi
echo ""

# Step 6: Delete ECR Repository
echo -e "${YELLOW}Step 6: Delete ECR Repository${NC}"
if aws ecr describe-repositories --repository-names ${ECR_REPO_NAME} --region ${REGION} 2>/dev/null >/dev/null; then
    aws ecr delete-repository \
        --repository-name ${ECR_REPO_NAME} \
        --region ${REGION} \
        --force >/dev/null
    echo -e "${GREEN}✓ ECR repo deleted${NC}"
else
    echo "  No ECR repo found"
fi
echo ""

# Step 6.5: Delete auto-created Gateway (only if we created it)
if [ "$GATEWAY_AUTO_CREATED" = "yes" ] && [ -n "$RESOLVED_GATEWAY_ID" ]; then
    echo -e "${YELLOW}Step 6.5: Delete auto-created Gateway${NC}"
    "$PY" -c "
import boto3
c = boto3.client('bedrock-agentcore-control', region_name='${REGION}')
c.delete_gateway(gatewayIdentifier='${RESOLVED_GATEWAY_ID}')
print('  ✓ Gateway deleted: ${RESOLVED_GATEWAY_ID}')
" 2>/dev/null || echo "  Failed to delete gateway"

    if aws iam get-role --role-name "${AUTO_GATEWAY_ROLE}" >/dev/null 2>&1; then
        aws iam delete-role-policy --role-name "${AUTO_GATEWAY_ROLE}" --policy-name gateway-default 2>/dev/null || true
        aws iam delete-role-policy --role-name "${AUTO_GATEWAY_ROLE}" --policy-name InvokeAgentRuntimePolicy 2>/dev/null || true
        aws iam delete-role --role-name "${AUTO_GATEWAY_ROLE}" 2>/dev/null && \
            echo -e "${GREEN}✓ Auto-created Gateway role deleted${NC}" || \
            echo "  Failed to delete gateway role"
    fi
    echo ""
fi

# Step 7: Delete IAM Roles
echo -e "${YELLOW}Step 7: Delete IAM Roles${NC}"

# Detach every managed policy and delete every inline policy on a role, then
# delete the role. Enumerating dynamically keeps cleanup in sync with deploy.sh
# no matter how many inline policies it adds (execution role gets ecr-and-logs,
# bedrock-invoke-model, otel-observability) — hardcoding names left the role
# undeletable (DeleteConflict) whenever deploy.sh grew a new policy.
delete_role_fully() {
    local role="$1"
    if ! aws iam get-role --role-name "${role}" 2>/dev/null >/dev/null; then
        echo "  No ${role} found"
        return
    fi
    for arn in $(aws iam list-attached-role-policies --role-name "${role}" \
        --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
        aws iam detach-role-policy --role-name "${role}" --policy-arn "${arn}" 2>/dev/null || true
    done
    for pol in $(aws iam list-role-policies --role-name "${role}" \
        --query 'PolicyNames[]' --output text 2>/dev/null); do
        aws iam delete-role-policy --role-name "${role}" --policy-name "${pol}" 2>/dev/null || true
    done
    if aws iam delete-role --role-name "${role}" 2>/dev/null; then
        echo -e "${GREEN}✓ ${role} deleted${NC}"
    else
        echo -e "${RED}  Failed to delete ${role}${NC}"
    fi
}

delete_role_fully "${EXECUTION_ROLE_NAME}"
delete_role_fully "${CODEBUILD_ROLE_NAME}"
echo ""

# Cleanup local files
if [ -f "deployment-info.log" ]; then
    rm deployment-info.log
    echo -e "${GREEN}✓ deployment-info.log removed${NC}"
fi

echo -e "\n${GREEN}=== Cleanup Complete ===${NC}"
