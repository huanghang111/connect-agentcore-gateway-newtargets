#!/bin/bash
# 清理脚本

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

STACK_NAME="connect-ac-api-stack"
REGION="us-west-2"
BUCKET_NAME="connect-workshop-ac-20260126"

echo -e "${YELLOW}=== 清理资源 ===${NC}\n"

# 删除CloudFormation stack
echo -e "\n${YELLOW}删除CloudFormation stack...${NC}"
aws cloudformation delete-stack \
  --stack-name ${STACK_NAME} \
  --region ${REGION}
echo -e "${GREEN}✓ Stack删除请求已提交${NC}"

echo -e "\n${YELLOW}等待Stack删除完成...${NC}"
aws cloudformation wait stack-delete-complete \
  --stack-name ${STACK_NAME} \
  --region ${REGION}
echo -e "${GREEN}✓ Stack删除完成${NC}"

# 删除S3 bucket (可选)
echo -e "\n${YELLOW}是否删除S3 bucket? (y/N)${NC}"
read -p "> " DELETE_BUCKET
if [ "$DELETE_BUCKET" = "y" ] || [ "$DELETE_BUCKET" = "Y" ]; then
    echo -e "${YELLOW}删除S3 bucket...${NC}"
    aws s3 rb s3://${BUCKET_NAME} --force --region ${REGION} 2>/dev/null || true
    echo -e "${GREEN}✓ Bucket删除完成${NC}"
else
    echo -e "${YELLOW}跳过S3 bucket删除${NC}"
fi

# 删除部署信息文件
if [ -f "deployment-info.log" ]; then
    rm deployment-info.log
    echo -e "${GREEN}✓ 部署信息文件已删除${NC}"
fi

echo -e "\n${GREEN}=== 清理完成 ===${NC}\n"
