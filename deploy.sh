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
STACK_NAME="connect-ac-api-stack"
REGION="us-west-2"
BUCKET_NAME="connect-workshop-ac-20260126"
OPENAPI_S3_URL="s3://${BUCKET_NAME}/connect-api-openapi.yaml"

echo -e "${YELLOW}=== Midea Repair Service API 部署 ===${NC}\n"

# 检查S3 bucket是否存在
echo -e "${YELLOW}步骤 1/4: 检查S3 bucket${NC}"
if aws s3 ls s3://${BUCKET_NAME} --region ${REGION} 2>/dev/null; then
    echo -e "${GREEN}✓ Bucket已存在: ${BUCKET_NAME}${NC}\n"
else
    echo -e "${YELLOW}Bucket不存在，正在创建...${NC}"
    aws s3 mb s3://${BUCKET_NAME} --region ${REGION}
    echo -e "${GREEN}✓ Bucket创建成功: ${BUCKET_NAME}${NC}\n"
fi

# 上传OpenAPI规范
echo -e "${YELLOW}步骤 2/4: 上传OpenAPI规范${NC}"
aws s3 cp connect-api-openapi.yaml s3://${BUCKET_NAME}/
echo -e "${GREEN}✓ OpenAPI规范上传成功${NC}\n"

# 创建CloudFormation stack
echo -e "${YELLOW}步骤 3/4: 创建CloudFormation stack${NC}"
echo "Stack名称: ${STACK_NAME}"
echo "区域: ${REGION}"
echo "OpenAPI URL: ${OPENAPI_S3_URL}"
echo ""

aws cloudformation create-stack \
  --stack-name ${STACK_NAME} \
  --template-body file://connect-api-customer.yaml \
  --parameters ParameterKey=OpenApiSpecUrl,ParameterValue=${OPENAPI_S3_URL} \
  --capabilities CAPABILITY_IAM \
  --region ${REGION}
echo -e "${GREEN}✓ Stack创建请求已提交${NC}\n"

# 等待部署完成
echo -e "${YELLOW}步骤 4/4: 等待部署完成 (可能需要3-5分钟)${NC}"
aws cloudformation wait stack-create-complete \
  --stack-name ${STACK_NAME} \
  --region ${REGION}
echo -e "${GREEN}✓ Stack部署完成${NC}\n"

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
