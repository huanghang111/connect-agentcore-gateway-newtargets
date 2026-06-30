#!/bin/bash
# Deploy MCP Server Agent to AgentCore Runtime (CodeBuild for image)
# Steps 1-5: Shell (ECR, S3, CodeBuild, IAM)
# Steps 6-8: Python via .venv (AgentCore Runtime, Gateway IAM, Gateway Target)

set -e

cd "$(dirname "$0")"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# ========================================
# CONFIGURATION (loaded from .env)
# ========================================
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

# ACCOUNT_ID is optional in .env on CloudShell — auto-resolve via STS.
if [ -z "${ACCOUNT_ID:-}" ]; then
    ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
fi
if [ -z "${ACCOUNT_ID}" ]; then
    echo -e "${RED}✗ ACCOUNT_ID is empty and STS lookup failed. Set ACCOUNT_ID in .env or run 'aws configure'.${NC}"
    exit 1
fi
export ACCOUNT_ID

# ========================================
# Auto-generate IDENTITY_TOKEN_SECRET on first deploy
# ========================================
# Persist a fresh 32-byte hex secret back into .env if it's empty, so every
# subsequent deploy reuses the SAME secret (otherwise in-flight customer
# identity tokens would be invalidated on every redeploy). To rotate, empty
# the line in .env and re-run this script.
if [ -z "${IDENTITY_TOKEN_SECRET:-}" ]; then
    NEW_SECRET="$(openssl rand -hex 32)"
    if grep -qE '^IDENTITY_TOKEN_SECRET=' .env; then
        # macOS/BSD sed and GNU sed differ on -i; write through a tmp file to be portable.
        awk -v val="$NEW_SECRET" 'BEGIN{FS=OFS="="} /^IDENTITY_TOKEN_SECRET=/{$0="IDENTITY_TOKEN_SECRET=" val} {print}' .env > .env.tmp && mv .env.tmp .env
    else
        printf '\nIDENTITY_TOKEN_SECRET=%s\n' "$NEW_SECRET" >> .env
    fi
    IDENTITY_TOKEN_SECRET="$NEW_SECRET"
    export IDENTITY_TOKEN_SECRET
    echo -e "${GREEN}✓ Generated and persisted IDENTITY_TOKEN_SECRET to .env (one-time)${NC}"
fi

CODEBUILD_PROJECT_NAME="${AGENT_NAME}-build"
S3_BUCKET="${AGENT_NAME}-source-${ACCOUNT_ID}"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO_NAME}"

echo -e "${YELLOW}=== MCP Agent Deployment ===${NC}\n"

# ========================================
# STEP 0: Resolve Python interpreter with a recent-enough boto3
# ========================================
# The deploy_runtime.py script needs the AgentCore Gateway Target API including
# `iamCredentialProvider`. Older boto3/botocore service-model data does not
# carry that field, so we MUST verify the schema, not just `import boto3`.
echo -e "${YELLOW}Step 0: Resolve Python interpreter${NC}"

check_schema() {
    "$1" - <<'PY' >/dev/null 2>&1
import sys, boto3
try:
    op = boto3.client("bedrock-agentcore-control", region_name="us-east-1") \
        .meta.service_model.operation_model("CreateGatewayTarget")
    cred_list = op.input_shape.members["credentialProviderConfigurations"]
    fields = cred_list.member.members["credentialProvider"].members
    sys.exit(0 if "iamCredentialProvider" in fields else 1)
except Exception:
    sys.exit(1)
PY
}

PY=""
if python3 -c 'import boto3' >/dev/null 2>&1 && check_schema "$(command -v python3)"; then
    PY="$(command -v python3)"
    echo -e "${GREEN}✓ Using system python3 (boto3 schema OK)${NC}\n"
else
    if python3 -c 'import boto3' >/dev/null 2>&1; then
        echo "  System boto3 is too old (missing iamCredentialProvider); falling back to .venv"
    fi
    if [ ! -x ".venv/bin/python" ]; then
        echo "  Creating .venv ..."
        python3 -m venv .venv
    fi
    if ! check_schema ".venv/bin/python"; then
        echo "  Installing/upgrading boto3 into .venv ..."
        .venv/bin/pip install --upgrade pip 'boto3>=1.43.0' 'botocore>=1.43.0' -q
    fi
    if ! check_schema ".venv/bin/python"; then
        echo -e "${RED}✗ Even after upgrade, boto3 schema does not contain iamCredentialProvider.${NC}"
        echo -e "${RED}  Check pip output above; you likely need: pip install -U boto3${NC}"
        exit 1
    fi
    PY=".venv/bin/python"
    echo -e "${GREEN}✓ venv ready${NC}\n"
fi
export PY

# ========================================
# STEP 1: ECR Repository
# ========================================
echo -e "${YELLOW}Step 1/8: ECR Repository${NC}"
if aws ecr describe-repositories --repository-names ${ECR_REPO_NAME} --region ${REGION} 2>/dev/null >/dev/null; then
    echo -e "${GREEN}✓ Already exists${NC}"
else
    aws ecr create-repository \
        --repository-name ${ECR_REPO_NAME} \
        --region ${REGION} \
        --image-scanning-configuration scanOnPush=true >/dev/null
    echo -e "${GREEN}✓ Created${NC}"
fi
echo ""

# ========================================
# STEP 2: Upload source to S3
# ========================================
echo -e "${YELLOW}Step 2/8: Upload source to S3${NC}"
if ! aws s3 ls s3://${S3_BUCKET} --region ${REGION} 2>/dev/null >/dev/null; then
    aws s3 mb s3://${S3_BUCKET} --region ${REGION} >/dev/null
fi

rm -f /tmp/mcp-agent-source.zip
zip -j /tmp/mcp-agent-source.zip mcp_server.py china_regions_pinyin.json Dockerfile requirements.txt buildspec.yml .dockerignore >/dev/null
# SOP skill files must keep their `skills/` directory, so add them WITHOUT -j.
zip -r /tmp/mcp-agent-source.zip skills -i 'skills/*.md' >/dev/null
aws s3 cp /tmp/mcp-agent-source.zip s3://${S3_BUCKET}/source.zip --region ${REGION} >/dev/null
rm -f /tmp/mcp-agent-source.zip
echo -e "${GREEN}✓ Source uploaded${NC}"
echo ""

# ========================================
# STEP 3: CodeBuild Project
# ========================================
echo -e "${YELLOW}Step 3/8: CodeBuild Project${NC}"

CODEBUILD_ROLE_NAME="${AGENT_NAME}-codebuild-role"

if ! aws iam get-role --role-name ${CODEBUILD_ROLE_NAME} 2>/dev/null >/dev/null; then
    CODEBUILD_TRUST='{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "codebuild.amazonaws.com"},
        "Action": "sts:AssumeRole"
      }]
    }'
    aws iam create-role \
        --role-name ${CODEBUILD_ROLE_NAME} \
        --assume-role-policy-document "${CODEBUILD_TRUST}" >/dev/null

    CODEBUILD_POLICY="{
      \"Version\": \"2012-10-17\",
      \"Statement\": [
        {
          \"Effect\": \"Allow\",
          \"Action\": [
            \"ecr:GetAuthorizationToken\",
            \"ecr:BatchCheckLayerAvailability\",
            \"ecr:GetDownloadUrlForLayer\",
            \"ecr:BatchGetImage\",
            \"ecr:PutImage\",
            \"ecr:InitiateLayerUpload\",
            \"ecr:UploadLayerPart\",
            \"ecr:CompleteLayerUpload\"
          ],
          \"Resource\": \"*\"
        },
        {
          \"Effect\": \"Allow\",
          \"Action\": [\"s3:GetObject\", \"s3:GetObjectVersion\"],
          \"Resource\": \"arn:aws:s3:::${S3_BUCKET}/*\"
        },
        {
          \"Effect\": \"Allow\",
          \"Action\": [\"logs:CreateLogGroup\", \"logs:CreateLogStream\", \"logs:PutLogEvents\"],
          \"Resource\": \"*\"
        }
      ]
    }"
    aws iam put-role-policy \
        --role-name ${CODEBUILD_ROLE_NAME} \
        --policy-name codebuild-policy \
        --policy-document "${CODEBUILD_POLICY}"
    echo "  IAM role created, waiting 10s..."
    sleep 10
fi
CODEBUILD_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${CODEBUILD_ROLE_NAME}"

if aws codebuild batch-get-projects --names ${CODEBUILD_PROJECT_NAME} --region ${REGION} \
    --query "projects[0].name" --output text 2>/dev/null | grep -q "${CODEBUILD_PROJECT_NAME}"; then
    echo -e "${GREEN}✓ Project exists${NC}"
else
    aws codebuild create-project \
        --name ${CODEBUILD_PROJECT_NAME} \
        --region ${REGION} \
        --source "{\"type\":\"S3\",\"location\":\"${S3_BUCKET}/source.zip\"}" \
        --artifacts "{\"type\":\"NO_ARTIFACTS\"}" \
        --environment "{\"type\":\"ARM_CONTAINER\",\"image\":\"aws/codebuild/amazonlinux2-aarch64-standard:3.0\",\"computeType\":\"BUILD_GENERAL1_SMALL\",\"privilegedMode\":true,\"environmentVariables\":[{\"name\":\"ACCOUNT_ID\",\"value\":\"${ACCOUNT_ID}\"},{\"name\":\"ECR_REPO_URI\",\"value\":\"${ECR_URI}\"},{\"name\":\"AWS_DEFAULT_REGION\",\"value\":\"${REGION}\"}]}" \
        --service-role ${CODEBUILD_ROLE_ARN} >/dev/null
    echo -e "${GREEN}✓ Project created${NC}"
fi
echo ""

# ========================================
# STEP 4: Run CodeBuild
# ========================================
echo -e "${YELLOW}Step 4/8: Build Docker image via CodeBuild${NC}"

BUILD_ID=$(aws codebuild start-build \
    --project-name ${CODEBUILD_PROJECT_NAME} \
    --region ${REGION} \
    --source-location-override "${S3_BUCKET}/source.zip" \
    --query "build.id" --output text)

echo "  Build ID: ${BUILD_ID}"
echo "  Waiting for build..."

for i in $(seq 1 60); do
    BUILD_STATUS=$(aws codebuild batch-get-builds \
        --ids ${BUILD_ID} \
        --region ${REGION} \
        --query "builds[0].buildStatus" --output text)
    if [ "$BUILD_STATUS" = "SUCCEEDED" ]; then
        echo -e "${GREEN}✓ Build succeeded${NC}"
        break
    elif [ "$BUILD_STATUS" = "FAILED" ] || [ "$BUILD_STATUS" = "FAULT" ] || [ "$BUILD_STATUS" = "STOPPED" ]; then
        echo -e "${RED}✗ Build failed: ${BUILD_STATUS}${NC}"
        aws codebuild batch-get-builds \
            --ids ${BUILD_ID} \
            --region ${REGION} \
            --query "builds[0].phases[?phaseStatus=='FAILED']" --output table
        exit 1
    fi
    echo "  Status: ${BUILD_STATUS} (${i}/60)"
    sleep 10
done
echo ""

# ========================================
# STEP 5: Runtime Execution Role
# ========================================
echo -e "${YELLOW}Step 5/8: Runtime Execution Role${NC}"
ROLE_NAME="${AGENT_NAME}-execution-role"

if aws iam get-role --role-name ${ROLE_NAME} 2>/dev/null >/dev/null; then
    echo -e "${GREEN}✓ Already exists${NC}"
else
    TRUST_POLICY='{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
        "Action": "sts:AssumeRole"
      }]
    }'
    aws iam create-role \
        --role-name ${ROLE_NAME} \
        --assume-role-policy-document "${TRUST_POLICY}" >/dev/null

    aws iam attach-role-policy \
        --role-name ${ROLE_NAME} \
        --policy-arn arn:aws:iam::aws:policy/BedrockAgentCoreFullAccess

    ECR_POLICY='{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Action": ["ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage", "ecr:GetAuthorizationToken"],
        "Resource": "*"
      },{
        "Effect": "Allow",
        "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": "*"
      }]
    }'
    aws iam put-role-policy \
        --role-name ${ROLE_NAME} \
        --policy-name ecr-and-logs \
        --policy-document "${ECR_POLICY}"

    echo "  Waiting 10s for IAM propagation..."
    sleep 10
    echo -e "${GREEN}✓ Created${NC}"
fi

# Ensure bedrock:InvokeModel is granted (response normalization in mcp_server.py
# calls bedrock-runtime). Run unconditionally — put-role-policy is idempotent —
# so existing roles created before this feature also get the permission.
BEDROCK_INVOKE_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": [
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:*:*:inference-profile/*",
      "arn:aws:bedrock:*:*:application-inference-profile/*"
    ]
  }]
}'
aws iam put-role-policy \
    --role-name ${ROLE_NAME} \
    --policy-name bedrock-invoke-model \
    --policy-document "${BEDROCK_INVOKE_POLICY}" >/dev/null
echo -e "${GREEN}✓ bedrock:InvokeModel granted${NC}"

# OTEL / AgentCore Observability — X-Ray PutTraceSegments + spans log group.
# BedrockAgentCoreFullAccess covers most of this, but we attach explicit ADOT
# permissions in case the managed policy lags behind. put-role-policy is
# idempotent.
OBSERVABILITY_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "xray:PutTraceSegments",
      "xray:PutSpans",
      "xray:PutSpansForIndexing",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
      "logs:DescribeLogGroups",
      "cloudwatch:PutMetricData"
    ],
    "Resource": "*"
  }]
}'
aws iam put-role-policy \
    --role-name ${ROLE_NAME} \
    --policy-name otel-observability \
    --policy-document "${OBSERVABILITY_POLICY}" >/dev/null
echo -e "${GREEN}✓ OTEL/X-Ray permissions granted${NC}"
echo ""

# ========================================
# STEPS 6-8: AgentCore Runtime + Gateway IAM + Gateway Target (Python)
# ========================================
echo -e "${YELLOW}Steps 6-8: AgentCore Runtime & Gateway Target${NC}"
"$PY" deploy_runtime.py

echo ""
echo -e "${GREEN}=== Done ===${NC}"
