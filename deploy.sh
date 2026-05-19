#!/bin/bash
# 部署脚本

set -e

# 切换到脚本所在目录
cd "$(dirname "$0")"

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 配置
# Override via env vars if needed:
#   STACK_NAME=foo REGION=us-west-2 ./deploy.sh
STACK_NAME="${STACK_NAME:-connect-repair-api-stack}"
REGION="${REGION:-us-east-1}"

# Resolve account ID from STS to avoid hard-coding someone else's account
# (important on AWS CloudShell where multiple users share scripts).
if [ -z "${AWS_ACCOUNT_ID:-}" ]; then
    AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
fi
if [ -z "${AWS_ACCOUNT_ID}" ]; then
    echo -e "${RED}✗ 无法获取 AWS 账号 ID。请确认 AWS CLI 已登录(CloudShell 自动登录)。${NC}"
    exit 1
fi

BUCKET_NAME="${BUCKET_NAME:-connect-repair-api-${AWS_ACCOUNT_ID}-${REGION}}"
OPENAPI_S3_URL="s3://${BUCKET_NAME}/connect-api-openapi.yaml"

echo -e "${YELLOW}=== Repair Service API 部署 ===${NC}\n"
echo "  Account: ${AWS_ACCOUNT_ID}"
echo "  Region:  ${REGION}"
echo "  Stack:   ${STACK_NAME}"
echo "  Bucket:  ${BUCKET_NAME}"
echo ""

# 检查S3 bucket是否存在
echo -e "${YELLOW}步骤 1/4: 检查S3 bucket${NC}"
if aws s3 ls s3://${BUCKET_NAME} --region ${REGION} 2>/dev/null; then
    echo -e "${GREEN}✓ Bucket已存在: ${BUCKET_NAME}${NC}\n"
else
    echo -e "${YELLOW}Bucket不存在，正在创建...${NC}"
    aws s3 mb s3://${BUCKET_NAME} --region ${REGION}
    echo -e "${GREEN}✓ Bucket创建成功: ${BUCKET_NAME}${NC}\n"
fi

# 上传OpenAPI规范 + 主模板（模板 > 51200 字节,必须走 S3）
echo -e "${YELLOW}步骤 2/4: 上传OpenAPI规范 + 主模板${NC}"
aws s3 cp connect-api-openapi.yaml s3://${BUCKET_NAME}/ --region ${REGION}
aws s3 cp connect-api-customer.yaml s3://${BUCKET_NAME}/ --region ${REGION}
TEMPLATE_S3_URL="https://${BUCKET_NAME}.s3.${REGION}.amazonaws.com/connect-api-customer.yaml"
echo -e "${GREEN}✓ 上传成功${NC}\n"

# 创建或更新CloudFormation stack
echo -e "${YELLOW}步骤 3/4: 部署CloudFormation stack${NC}"
echo "Stack名称: ${STACK_NAME}"
echo "区域: ${REGION}"
echo "OpenAPI URL: ${OPENAPI_S3_URL}"
echo ""

if aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" >/dev/null 2>&1; then
    echo "Stack 已存在，执行更新..."
    set +e
    UPDATE_OUT=$(aws cloudformation update-stack \
      --stack-name ${STACK_NAME} \
      --template-url ${TEMPLATE_S3_URL} \
      --parameters ParameterKey=OpenApiSpecUrl,ParameterValue=${OPENAPI_S3_URL} \
      --capabilities CAPABILITY_IAM \
      --region ${REGION} 2>&1)
    UPDATE_RC=$?
    set -e
    if [ $UPDATE_RC -ne 0 ]; then
        if echo "$UPDATE_OUT" | grep -q "No updates are to be performed"; then
            echo -e "${GREEN}✓ 模板无变化，跳过更新${NC}\n"
            WAIT_OP=""
        else
            echo "$UPDATE_OUT"
            exit 1
        fi
    else
        echo -e "${GREEN}✓ Stack 更新请求已提交${NC}\n"
        WAIT_OP="stack-update-complete"
    fi
else
    aws cloudformation create-stack \
      --stack-name ${STACK_NAME} \
      --template-url ${TEMPLATE_S3_URL} \
      --parameters ParameterKey=OpenApiSpecUrl,ParameterValue=${OPENAPI_S3_URL} \
      --capabilities CAPABILITY_IAM \
      --region ${REGION}
    echo -e "${GREEN}✓ Stack 创建请求已提交${NC}\n"
    WAIT_OP="stack-create-complete"
fi

if [ -n "$WAIT_OP" ]; then
    echo -e "${YELLOW}步骤 4/4: 等待部署完成 (可能需要3-5分钟)${NC}"
    aws cloudformation wait $WAIT_OP --stack-name ${STACK_NAME} --region ${REGION}
    echo -e "${GREEN}✓ Stack 部署完成${NC}\n"
fi

# 获取输出信息
echo -e "${YELLOW}获取API信息...${NC}"
API_URL=$(aws cloudformation describe-stacks \
  --stack-name ${STACK_NAME} \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiUrl`].OutputValue' \
  --output text \
  --region ${REGION})

API_KEY=$(aws cloudformation describe-stacks \
  --stack-name ${STACK_NAME} \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiKey`].OutputValue' \
  --output text \
  --region ${REGION})

TABLE_NAME=$(aws cloudformation describe-stacks \
  --stack-name ${STACK_NAME} \
  --query 'Stacks[0].Outputs[?OutputKey==`RepairTicketsTableName`].OutputValue' \
  --output text \
  --region ${REGION})

echo -e "${GREEN}✓ 部署信息获取成功${NC}\n"

# 保存到文件
cat > deployment-info.log << EOF
=== Midea Repair Service API 部署信息 ===

部署时间: $(date)
Stack名称: ${STACK_NAME}
AWS区域: ${REGION}
S3 Bucket: ${BUCKET_NAME}

API URL: ${API_URL}
API Key: ${API_KEY}
DynamoDB表: ${TABLE_NAME}

=== 测试命令 ===

# 设置环境变量
export API_URL="${API_URL}"
export API_KEY="${API_KEY}"

# 运行测试
./test-api.sh

=== API端点 ===

1. 创建维修工单
   POST ${API_URL}/repair/request

2. 查询工单状态
   POST ${API_URL}/repair/track

3. FAQ查询
   POST ${API_URL}/faq/simple

=== 清理命令 ===

# 删除Stack
aws cloudformation delete-stack --stack-name ${STACK_NAME} --region ${REGION}

# 删除S3 bucket (可选)
aws s3 rb s3://${BUCKET_NAME} --force --region ${REGION}
EOF

echo -e "${GREEN}=== 部署完成 ===${NC}\n"
echo "部署信息已保存到: deployment-info.log"
echo ""
echo -e "${YELLOW}下一步:${NC}"
echo "1. 查看部署信息: cat deployment-info.log"
echo "2. 运行测试: ./test-api.sh"
echo ""
