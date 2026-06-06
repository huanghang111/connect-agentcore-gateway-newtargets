#!/bin/bash
# Unified deploy script — provisions everything except Amazon Connect:
#   1. Backend Repair API (CloudFormation: API Gateway + Lambda + DynamoDB)
#   2. MCP Agent on AgentCore Runtime (ECR + CodeBuild ARM64 image)
#   3. AgentCore Gateway with authorizerType=NONE (testing only) + mcpServer target
#
# Run from the directory containing this script. Configuration comes from
# the local `.env` (copy `.env.example` to `.env` first).

set -e
cd "$(dirname "$0")"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# ========================================
# Load .env
# ========================================
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        echo -e "${YELLOW}.env not found — copying .env.example to .env${NC}"
        cp .env.example .env
        echo -e "${YELLOW}Edit .env if you need to override defaults, then re-run ./deploy.sh${NC}"
    else
        echo -e "${RED}✗ Neither .env nor .env.example exists.${NC}"
        exit 1
    fi
fi
set -a
# shellcheck disable=SC1091
source .env
set +a

REGION="${REGION:-us-east-1}"

if [ -z "${ACCOUNT_ID:-}" ]; then
    ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
fi
if [ -z "${ACCOUNT_ID}" ]; then
    echo -e "${RED}✗ Cannot resolve AWS account. Configure AWS CLI (or run in CloudShell).${NC}"
    exit 1
fi
export ACCOUNT_ID

STACK_NAME="${STACK_NAME:-connect-repair-api-stack}"
BUCKET_NAME="${BUCKET_NAME:-connect-repair-api-${ACCOUNT_ID}-${REGION}}"
AGENT_NAME="${AGENT_NAME:-connect-repair-mcp-server}"
ECR_REPO_NAME="${ECR_REPO_NAME:-connect-repair-mcp-server}"
TARGET_NAME="${TARGET_NAME:-connect-repair-mcp-agent}"

CODEBUILD_PROJECT_NAME="${AGENT_NAME}-build"
S3_BUCKET="${AGENT_NAME}-source-${ACCOUNT_ID}"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO_NAME}"
OPENAPI_S3_URL="s3://${BUCKET_NAME}/connect-api-openapi.yaml"
TEMPLATE_S3_URL="https://${BUCKET_NAME}.s3.${REGION}.amazonaws.com/connect-api-customer.yaml"

echo -e "${YELLOW}=== Connect Repair Stack — unified deploy ===${NC}"
echo "  Account:       ${ACCOUNT_ID}"
echo "  Region:        ${REGION}"
echo "  Backend stack: ${STACK_NAME}"
echo "  API bucket:    ${BUCKET_NAME}"
echo "  Agent name:    ${AGENT_NAME}"
echo ""

# ========================================
# Resolve a Python interpreter with a fresh enough boto3 (must expose
# iamCredentialProvider on CreateGatewayTarget). Reuse the .venv that the
# old per-component script used so users with that already provisioned skip
# the install.
# ========================================
echo -e "${YELLOW}Step 0/12: Resolve Python interpreter${NC}"
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
    echo -e "${GREEN}✓ Using system python3${NC}"
else
    if [ ! -x ".venv/bin/python" ]; then
        echo "  Creating .venv ..."
        python3 -m venv .venv
    fi
    if ! check_schema ".venv/bin/python"; then
        echo "  Installing/upgrading boto3 into .venv ..."
        .venv/bin/pip install --upgrade pip 'boto3>=1.43.0' 'botocore>=1.43.0' -q
    fi
    if ! check_schema ".venv/bin/python"; then
        echo -e "${RED}✗ boto3 schema still missing iamCredentialProvider after upgrade.${NC}"
        exit 1
    fi
    PY=".venv/bin/python"
    echo -e "${GREEN}✓ venv ready${NC}"
fi
export PY
echo ""

# ========================================================================
# PART A — Backend Repair API (CloudFormation)
# ========================================================================
echo -e "${YELLOW}=== PART A: Backend Repair API ===${NC}"

# Step A1: S3 bucket for CFN templates
echo -e "${YELLOW}Step 1/12: S3 bucket for CFN templates${NC}"
if aws s3 ls "s3://${BUCKET_NAME}" --region "${REGION}" >/dev/null 2>&1; then
    echo -e "${GREEN}✓ Bucket exists: ${BUCKET_NAME}${NC}"
else
    aws s3 mb "s3://${BUCKET_NAME}" --region "${REGION}"
    echo -e "${GREEN}✓ Bucket created${NC}"
fi
echo ""

# Step A2: Upload OpenAPI + main template
echo -e "${YELLOW}Step 2/12: Upload OpenAPI + CFN template${NC}"
aws s3 cp connect-api-openapi.yaml "s3://${BUCKET_NAME}/" --region "${REGION}" >/dev/null
aws s3 cp connect-api-customer.yaml "s3://${BUCKET_NAME}/" --region "${REGION}" >/dev/null
echo -e "${GREEN}✓ Uploaded${NC}"
echo ""

# Step A3: Create / update CFN stack
echo -e "${YELLOW}Step 3/12: Deploy CloudFormation stack${NC}"
if aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" >/dev/null 2>&1; then
    echo "  Stack exists — updating..."
    set +e
    UPDATE_OUT=$(aws cloudformation update-stack \
        --stack-name "${STACK_NAME}" \
        --template-url "${TEMPLATE_S3_URL}" \
        --parameters ParameterKey=OpenApiSpecUrl,ParameterValue="${OPENAPI_S3_URL}" \
        --capabilities CAPABILITY_IAM \
        --region "${REGION}" 2>&1)
    UPDATE_RC=$?
    set -e
    if [ $UPDATE_RC -ne 0 ]; then
        if echo "$UPDATE_OUT" | grep -q "No updates are to be performed"; then
            echo -e "${GREEN}✓ No template changes${NC}"
            WAIT_OP=""
        else
            echo "$UPDATE_OUT"
            exit 1
        fi
    else
        echo -e "${GREEN}✓ Update submitted${NC}"
        WAIT_OP="stack-update-complete"
    fi
else
    aws cloudformation create-stack \
        --stack-name "${STACK_NAME}" \
        --template-url "${TEMPLATE_S3_URL}" \
        --parameters ParameterKey=OpenApiSpecUrl,ParameterValue="${OPENAPI_S3_URL}" \
        --capabilities CAPABILITY_IAM \
        --region "${REGION}" >/dev/null
    echo -e "${GREEN}✓ Create submitted${NC}"
    WAIT_OP="stack-create-complete"
fi

if [ -n "$WAIT_OP" ]; then
    echo "  Waiting for stack (3-5 min)..."
    aws cloudformation wait $WAIT_OP --stack-name "${STACK_NAME}" --region "${REGION}"
    echo -e "${GREEN}✓ Stack ready${NC}"
fi
echo ""

# Step A4: Read stack outputs back into env (and persist to .env so re-runs / cleanup pick them up)
echo -e "${YELLOW}Step 4/12: Read API URL / API Key from stack outputs${NC}"
API_URL=$(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --query 'Stacks[0].Outputs[?OutputKey==`ApiUrl`].OutputValue' \
    --output text --region "${REGION}")
API_KEY=$(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --query 'Stacks[0].Outputs[?OutputKey==`ApiKey`].OutputValue' \
    --output text --region "${REGION}")
TABLE_NAME=$(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --query 'Stacks[0].Outputs[?OutputKey==`RepairTicketsTableName`].OutputValue' \
    --output text --region "${REGION}")

if [ -z "$API_URL" ] || [ -z "$API_KEY" ]; then
    echo -e "${RED}✗ Could not read ApiUrl / ApiKey from stack outputs${NC}"
    exit 1
fi

REPAIR_API_URL="$API_URL"
REPAIR_API_KEY="$API_KEY"
export REPAIR_API_URL REPAIR_API_KEY

# Persist back to .env so the Python step + future cleanup can read them.
update_env_var() {
    local key="$1"
    local val="$2"
    if grep -qE "^${key}=" .env; then
        awk -v k="$key" -v v="$val" 'BEGIN{FS=OFS="="} $1==k{$0=k"="v} {print}' .env > .env.tmp && mv .env.tmp .env
    else
        printf '%s=%s\n' "$key" "$val" >> .env
    fi
}
update_env_var REPAIR_API_URL "$REPAIR_API_URL"
update_env_var REPAIR_API_KEY "$REPAIR_API_KEY"
echo -e "${GREEN}✓ REPAIR_API_URL / REPAIR_API_KEY synced into .env${NC}"
echo ""

# ========================================
# IDENTITY_TOKEN_SECRET — generate once, persist to .env, reuse forever
# ========================================
if [ -z "${IDENTITY_TOKEN_SECRET:-}" ]; then
    NEW_SECRET="$(openssl rand -hex 32)"
    update_env_var IDENTITY_TOKEN_SECRET "$NEW_SECRET"
    IDENTITY_TOKEN_SECRET="$NEW_SECRET"
    export IDENTITY_TOKEN_SECRET
    echo -e "${GREEN}✓ Generated and persisted IDENTITY_TOKEN_SECRET (one-time)${NC}"
    echo ""
fi

# ========================================================================
# PART B — MCP Agent (ECR + CodeBuild + Runtime + Gateway + Target)
# ========================================================================
echo -e "${YELLOW}=== PART B: MCP Agent ===${NC}"

# Step B1: ECR repository
echo -e "${YELLOW}Step 5/12: ECR repository${NC}"
if aws ecr describe-repositories --repository-names "${ECR_REPO_NAME}" --region "${REGION}" >/dev/null 2>&1; then
    echo -e "${GREEN}✓ Already exists${NC}"
else
    aws ecr create-repository \
        --repository-name "${ECR_REPO_NAME}" \
        --region "${REGION}" \
        --image-scanning-configuration scanOnPush=true >/dev/null
    echo -e "${GREEN}✓ Created${NC}"
fi
echo ""

# Step B2: Upload mcp-agent source to S3
echo -e "${YELLOW}Step 6/12: Upload MCP Agent source to S3${NC}"
if ! aws s3 ls "s3://${S3_BUCKET}" --region "${REGION}" >/dev/null 2>&1; then
    aws s3 mb "s3://${S3_BUCKET}" --region "${REGION}" >/dev/null
fi
( cd mcp-agent && \
  zip -j /tmp/mcp-agent-source.zip mcp_server.py china_regions_pinyin.json Dockerfile requirements.txt buildspec.yml .dockerignore >/dev/null )
aws s3 cp /tmp/mcp-agent-source.zip "s3://${S3_BUCKET}/source.zip" --region "${REGION}" >/dev/null
rm -f /tmp/mcp-agent-source.zip
echo -e "${GREEN}✓ Source uploaded${NC}"
echo ""

# Step B3: CodeBuild project + IAM role
echo -e "${YELLOW}Step 7/12: CodeBuild project${NC}"
CODEBUILD_ROLE_NAME="${AGENT_NAME}-codebuild-role"

if ! aws iam get-role --role-name "${CODEBUILD_ROLE_NAME}" >/dev/null 2>&1; then
    CODEBUILD_TRUST='{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "codebuild.amazonaws.com"},
        "Action": "sts:AssumeRole"
      }]
    }'
    aws iam create-role \
        --role-name "${CODEBUILD_ROLE_NAME}" \
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
        --role-name "${CODEBUILD_ROLE_NAME}" \
        --policy-name codebuild-policy \
        --policy-document "${CODEBUILD_POLICY}"
    echo "  IAM role created, waiting 10s for propagation..."
    sleep 10
fi
CODEBUILD_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${CODEBUILD_ROLE_NAME}"

if aws codebuild batch-get-projects --names "${CODEBUILD_PROJECT_NAME}" --region "${REGION}" \
    --query "projects[0].name" --output text 2>/dev/null | grep -q "${CODEBUILD_PROJECT_NAME}"; then
    echo -e "${GREEN}✓ Project exists${NC}"
else
    aws codebuild create-project \
        --name "${CODEBUILD_PROJECT_NAME}" \
        --region "${REGION}" \
        --source "{\"type\":\"S3\",\"location\":\"${S3_BUCKET}/source.zip\"}" \
        --artifacts "{\"type\":\"NO_ARTIFACTS\"}" \
        --environment "{\"type\":\"ARM_CONTAINER\",\"image\":\"aws/codebuild/amazonlinux2-aarch64-standard:3.0\",\"computeType\":\"BUILD_GENERAL1_SMALL\",\"privilegedMode\":true,\"environmentVariables\":[{\"name\":\"ACCOUNT_ID\",\"value\":\"${ACCOUNT_ID}\"},{\"name\":\"ECR_REPO_URI\",\"value\":\"${ECR_URI}\"},{\"name\":\"AWS_DEFAULT_REGION\",\"value\":\"${REGION}\"}]}" \
        --service-role "${CODEBUILD_ROLE_ARN}" >/dev/null
    echo -e "${GREEN}✓ Project created${NC}"
fi
echo ""

# Step B4: Run CodeBuild
echo -e "${YELLOW}Step 8/12: Build ARM64 Docker image${NC}"
BUILD_ID=$(aws codebuild start-build \
    --project-name "${CODEBUILD_PROJECT_NAME}" \
    --region "${REGION}" \
    --source-location-override "${S3_BUCKET}/source.zip" \
    --query "build.id" --output text)
echo "  Build ID: ${BUILD_ID}"

for i in $(seq 1 60); do
    BUILD_STATUS=$(aws codebuild batch-get-builds \
        --ids "${BUILD_ID}" \
        --region "${REGION}" \
        --query "builds[0].buildStatus" --output text)
    if [ "$BUILD_STATUS" = "SUCCEEDED" ]; then
        echo -e "${GREEN}✓ Build succeeded${NC}"
        break
    elif [ "$BUILD_STATUS" = "FAILED" ] || [ "$BUILD_STATUS" = "FAULT" ] || [ "$BUILD_STATUS" = "STOPPED" ]; then
        echo -e "${RED}✗ Build failed: ${BUILD_STATUS}${NC}"
        aws codebuild batch-get-builds \
            --ids "${BUILD_ID}" \
            --region "${REGION}" \
            --query "builds[0].phases[?phaseStatus=='FAILED']" --output table
        exit 1
    fi
    echo "  Status: ${BUILD_STATUS} (${i}/60)"
    sleep 10
done
echo ""

# Step B5: Runtime execution role
echo -e "${YELLOW}Step 9/12: Runtime execution role${NC}"
ROLE_NAME="${AGENT_NAME}-execution-role"

if aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
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
        --role-name "${ROLE_NAME}" \
        --assume-role-policy-document "${TRUST_POLICY}" >/dev/null

    aws iam attach-role-policy \
        --role-name "${ROLE_NAME}" \
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
        --role-name "${ROLE_NAME}" \
        --policy-name ecr-and-logs \
        --policy-document "${ECR_POLICY}"

    echo "  Waiting 10s for IAM propagation..."
    sleep 10
    echo -e "${GREEN}✓ Created${NC}"
fi

# bedrock:InvokeModel — required by mcp_server.py response normalization (Strands → Bedrock).
# put-role-policy is idempotent, so this fixes pre-existing roles too.
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
    --role-name "${ROLE_NAME}" \
    --policy-name bedrock-invoke-model \
    --policy-document "${BEDROCK_INVOKE_POLICY}" >/dev/null
echo -e "${GREEN}✓ bedrock:InvokeModel granted${NC}"

# OTEL / X-Ray observability (ADOT)
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
    --role-name "${ROLE_NAME}" \
    --policy-name otel-observability \
    --policy-document "${OBSERVABILITY_POLICY}" >/dev/null
echo -e "${GREEN}✓ OTEL/X-Ray permissions granted${NC}"
echo ""

# Steps B6-B8: Runtime + Gateway + Target (Python — needs the full boto3 schema)
echo -e "${YELLOW}Steps 10-12: AgentCore Runtime, Gateway, Target${NC}"
"$PY" deploy_runtime.py
echo ""

# ========================================================================
# Final summary — write everything to deployment-info.log
# ========================================================================
RUNTIME_INFO_FILE="deployment-info.log"
{
    echo "=== Connect Repair — unified deployment info ==="
    echo "Deploy time: $(date)"
    echo ""
    echo "[Backend Repair API]"
    echo "Stack:          ${STACK_NAME}"
    echo "Region:         ${REGION}"
    echo "Bucket:         ${BUCKET_NAME}"
    echo "API URL:        ${REPAIR_API_URL}"
    echo "API Key:        ${REPAIR_API_KEY}"
    echo "DynamoDB table: ${TABLE_NAME}"
    echo ""
    echo "[MCP Agent]"
    if [ -f "deployment-info-runtime.log" ]; then
        cat deployment-info-runtime.log
    fi
    echo ""
    echo "[Test the backend API]"
    echo "  export API_URL=\"${REPAIR_API_URL}\""
    echo "  export API_KEY=\"${REPAIR_API_KEY}\""
    echo "  ./test-api.sh"
    echo ""
    echo "[Cleanup]"
    echo "  ./cleanup.sh    # tears down everything in reverse order"
} > "${RUNTIME_INFO_FILE}"

echo -e "${GREEN}=== All done ===${NC}"
echo "Deployment summary written to ${RUNTIME_INFO_FILE}"
