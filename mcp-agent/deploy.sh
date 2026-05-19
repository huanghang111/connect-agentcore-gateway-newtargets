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
: "${ACCOUNT_ID:?ACCOUNT_ID is required in .env}"
: "${AGENT_NAME:?AGENT_NAME is required in .env}"
: "${ECR_REPO_NAME:?ECR_REPO_NAME is required in .env}"

CODEBUILD_PROJECT_NAME="${AGENT_NAME}-build"
S3_BUCKET="${AGENT_NAME}-source-${ACCOUNT_ID}"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO_NAME}"

echo -e "${YELLOW}=== MCP Agent Deployment ===${NC}\n"

# ========================================
# STEP 0: Resolve Python interpreter with boto3
# ========================================
# Use the system python if it already has boto3 (e.g. AWS CloudShell);
# otherwise create / repair a local venv. Either way PY ends up pointing at
# an interpreter where `import boto3` works.
echo -e "${YELLOW}Step 0: Resolve Python interpreter${NC}"
if python3 -c 'import boto3' >/dev/null 2>&1; then
    PY="$(command -v python3)"
    echo -e "${GREEN}✓ Using system python3 (boto3 already installed)${NC}\n"
else
    if [ ! -x ".venv/bin/python" ]; then
        echo "  Creating .venv ..."
        python3 -m venv .venv
    fi
    if ! .venv/bin/python -c 'import boto3' >/dev/null 2>&1; then
        echo "  Installing boto3 into .venv ..."
        .venv/bin/pip install --upgrade pip boto3 -q
    fi
    if ! .venv/bin/python -c 'import boto3' >/dev/null 2>&1; then
        echo -e "${RED}✗ Failed to install boto3 into .venv${NC}"
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

zip -j /tmp/mcp-agent-source.zip mcp_server.py Dockerfile requirements.txt buildspec.yml .dockerignore >/dev/null
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
echo ""

# ========================================
# STEPS 6-8: AgentCore Runtime + Gateway IAM + Gateway Target (Python)
# ========================================
echo -e "${YELLOW}Steps 6-8: AgentCore Runtime & Gateway Target${NC}"
"$PY" deploy_runtime.py

echo ""
echo -e "${GREEN}=== Done ===${NC}"
