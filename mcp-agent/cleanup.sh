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
: "${ACCOUNT_ID:?ACCOUNT_ID is required in .env}"
: "${AGENT_NAME:?AGENT_NAME is required in .env}"
: "${ECR_REPO_NAME:?ECR_REPO_NAME is required in .env}"
: "${GATEWAY_ID:?GATEWAY_ID is required in .env}"
: "${GATEWAY_SERVICE_ROLE:?GATEWAY_SERVICE_ROLE is required in .env}"
: "${TARGET_NAME:?TARGET_NAME is required in .env}"

RUNTIME_NAME="${AGENT_NAME//-/_}"
CODEBUILD_PROJECT_NAME="${AGENT_NAME}-build"
CODEBUILD_ROLE_NAME="${AGENT_NAME}-codebuild-role"
S3_BUCKET="${AGENT_NAME}-source-${ACCOUNT_ID}"
EXECUTION_ROLE_NAME="${AGENT_NAME}-execution-role"

echo -e "${YELLOW}=== MCP Agent Cleanup ===${NC}\n"

# Step 1: Remove Gateway Target
echo -e "${YELLOW}Step 1: Remove Gateway Target${NC}"
if [ ! -d ".venv" ]; then
    echo "  No .venv found, creating for boto3..."
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip boto3 -q
fi

TARGET_ID=$(.venv/bin/python -c "
import boto3
client = boto3.client('bedrock-agentcore-control', region_name='${REGION}')
resp = client.list_gateway_targets(gatewayIdentifier='${GATEWAY_ID}')
for t in resp.get('targets', []):
    if t['name'] == '${TARGET_NAME}':
        print(t['targetId'])
        break
" 2>/dev/null || echo "")

if [ -n "$TARGET_ID" ]; then
    .venv/bin/python -c "
import boto3
client = boto3.client('bedrock-agentcore-control', region_name='${REGION}')
client.delete_gateway_target(gatewayIdentifier='${GATEWAY_ID}', targetId='${TARGET_ID}')
print('  ✓ Gateway target deleted: ${TARGET_ID}')
" 2>/dev/null || echo "  Failed to delete target"
else
    echo "  No target found"
fi
echo ""

# Step 2: Remove Gateway IAM inline policy
echo -e "${YELLOW}Step 2: Remove Gateway IAM Policy${NC}"
aws iam delete-role-policy \
    --role-name ${GATEWAY_SERVICE_ROLE} \
    --policy-name InvokeAgentRuntimePolicy 2>/dev/null && \
    echo -e "${GREEN}✓ Gateway IAM policy removed${NC}" || \
    echo "  No policy found"
echo ""

# Step 3: Delete AgentCore Runtime
echo -e "${YELLOW}Step 3: Delete AgentCore Runtime${NC}"
RUNTIME_ID=$(.venv/bin/python -c "
import boto3
client = boto3.client('bedrock-agentcore-control', region_name='${REGION}')
resp = client.list_agent_runtimes()
for rt in resp.get('agentRuntimes', []):
    if rt['agentRuntimeName'] == '${RUNTIME_NAME}':
        print(rt['agentRuntimeId'])
        break
" 2>/dev/null || echo "")

if [ -n "$RUNTIME_ID" ]; then
    .venv/bin/python -c "
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

# Step 7: Delete IAM Roles
echo -e "${YELLOW}Step 7: Delete IAM Roles${NC}"

# Execution role
if aws iam get-role --role-name ${EXECUTION_ROLE_NAME} 2>/dev/null >/dev/null; then
    aws iam delete-role-policy --role-name ${EXECUTION_ROLE_NAME} --policy-name ecr-and-logs 2>/dev/null || true
    aws iam detach-role-policy --role-name ${EXECUTION_ROLE_NAME} \
        --policy-arn arn:aws:iam::aws:policy/BedrockAgentCoreFullAccess 2>/dev/null || true
    aws iam delete-role --role-name ${EXECUTION_ROLE_NAME}
    echo -e "${GREEN}✓ Execution role deleted${NC}"
else
    echo "  No execution role found"
fi

# CodeBuild role
if aws iam get-role --role-name ${CODEBUILD_ROLE_NAME} 2>/dev/null >/dev/null; then
    aws iam delete-role-policy --role-name ${CODEBUILD_ROLE_NAME} --policy-name codebuild-policy 2>/dev/null || true
    aws iam delete-role --role-name ${CODEBUILD_ROLE_NAME}
    echo -e "${GREEN}✓ CodeBuild role deleted${NC}"
else
    echo "  No CodeBuild role found"
fi
echo ""

# Cleanup local files
if [ -f "deployment-info.log" ]; then
    rm deployment-info.log
    echo -e "${GREEN}✓ deployment-info.log removed${NC}"
fi

echo -e "\n${GREEN}=== Cleanup Complete ===${NC}"
