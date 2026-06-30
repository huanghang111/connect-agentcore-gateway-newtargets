#!/bin/bash
# API测试脚本

set -e

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 检查环境变量
if [ -z "$API_URL" ] || [ -z "$API_KEY" ]; then
    echo -e "${RED}错误: 请先设置环境变量${NC}"
    echo "export API_URL=\"<your-api-url>\""
    echo "export API_KEY=\"<your-api-key>\""
    exit 1
fi

echo -e "${YELLOW}=== Midea Repair Service API 测试 ===${NC}\n"
echo "API URL: $API_URL"
echo "API Key: ${API_KEY:0:10}..."
echo ""

# 测试用手机号（= MCP 层解析出的 customerId，后端作为 phone 持久化，供 listRepairs 检索）
TEST_PHONE="13800138000"

# 测试1: 创建维修工单
echo -e "${YELLOW}测试 1: 创建维修工单${NC}"
RESPONSE=$(curl -s -X POST "$API_URL/repair/request" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"productCategory\": \"Refrigerator\",
    \"productsubCategory\": \"French Door\",
    \"productModel\": \"MF200W90BE\",
    \"serialNumber\": \"SN20231015ABC123\",
    \"brand\": \"AnyCompany\",
    \"province\": \"NY\",
    \"city\": \"New York\",
    \"district\": \"Manhattan\",
    \"phone\": \"$TEST_PHONE\",
    \"description\": \"Refrigerator not cooling properly, temperature not going down. Customer already tried unplugging for 10 minutes.\"
  }")

if echo "$RESPONSE" | grep -q "ticketNumber"; then
    TICKET_NUMBER=$(echo "$RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['ticketNumber'], end='')")
    echo -e "${GREEN}✓ 工单创建成功${NC}"
    echo "  工单号: $TICKET_NUMBER"
else
    echo -e "${RED}✗ 工单创建失败${NC}"
    echo "$RESPONSE"
    exit 1
fi
echo ""

# 等待1秒
sleep 1

# 测试2: 查询工单状态
echo -e "${YELLOW}测试 2: 查询工单状态${NC}"
JSON_DATA=$(printf '{"woNumber":"%s"}' "$TICKET_NUMBER")
RESPONSE=$(curl -s -X POST "$API_URL/repair/track" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "$JSON_DATA")

if echo "$RESPONSE" | grep -q "Repair ticket found"; then
    STATUS=$(echo "$RESPONSE" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
    echo -e "${GREEN}✓ 工单查询成功${NC}"
    echo "  工单号: $TICKET_NUMBER"
    echo "  状态: $STATUS"
else
    echo -e "${RED}✗ 工单查询失败${NC}"
    echo "$RESPONSE"
    exit 1
fi
echo ""

# 测试3: FAQ查询 - 空调重置
echo -e "${YELLOW}测试 3: FAQ查询 - 空调重置${NC}"
RESPONSE=$(curl -s -X POST "$API_URL/faq/simple" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "How do I reset my air conditioner?"}')

if echo "$RESPONSE" | grep -q "results"; then
    COUNT=$(echo "$RESPONSE" | grep -o '"count":[0-9]*' | cut -d':' -f2)
    echo -e "${GREEN}✓ FAQ查询成功${NC}"
    echo "  找到 $COUNT 条相关结果"
else
    echo -e "${RED}✗ FAQ查询失败${NC}"
    echo "$RESPONSE"
    exit 1
fi
echo ""

# 测试4: FAQ查询 - 冰箱噪音
echo -e "${YELLOW}测试 4: FAQ查询 - 冰箱噪音${NC}"
RESPONSE=$(curl -s -X POST "$API_URL/faq/simple" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "refrigerator making noise"}')

if echo "$RESPONSE" | grep -q "results"; then
    COUNT=$(echo "$RESPONSE" | grep -o '"count":[0-9]*' | cut -d':' -f2)
    echo -e "${GREEN}✓ FAQ查询成功${NC}"
    echo "  找到 $COUNT 条相关结果"
else
    echo -e "${RED}✗ FAQ查询失败${NC}"
    echo "$RESPONSE"
    exit 1
fi
echo ""

# 测试5: 错误处理 - 缺少必需参数
echo -e "${YELLOW}测试 5: 错误处理 - 缺少必需参数${NC}"
RESPONSE=$(curl -s -X POST "$API_URL/repair/request" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"productCategory": "Refrigerator"}')

if echo "$RESPONSE" | grep -q "Missing required fields"; then
    echo -e "${GREEN}✓ 错误处理正确${NC}"
    echo "  正确返回缺少参数错误"
else
    echo -e "${RED}✗ 错误处理异常${NC}"
    echo "$RESPONSE"
fi
echo ""

# 测试6: 错误处理 - 工单不存在
echo -e "${YELLOW}测试 6: 错误处理 - 工单不存在${NC}"
RESPONSE=$(curl -s -X POST "$API_URL/repair/track" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  --data-raw '{"woNumber":"9999999999"}')

if echo "$RESPONSE" | grep -q "not found"; then
    echo -e "${GREEN}✓ 错误处理正确${NC}"
    echo "  正确返回工单不存在错误"
else
    echo -e "${RED}✗ 错误处理异常${NC}"
    echo "$RESPONSE"
fi
echo ""

# 测试7: 错误处理 - 非法 woNumber 格式
echo -e "${YELLOW}测试 7: 错误处理 - 非法 woNumber${NC}"
RESPONSE=$(curl -s -X POST "$API_URL/repair/track" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  --data-raw '{"woNumber":"abc"}')

if echo "$RESPONSE" | grep -q "10-digit"; then
    echo -e "${GREEN}✓ 错误处理正确${NC}"
else
    echo -e "${RED}✗ 错误处理异常${NC}"
    echo "$RESPONSE"
fi
echo ""

# 测试 7b: 工单列表 - 按手机号查到刚创建的工单
echo -e "${YELLOW}测试 7b: 工单列表（按手机号）${NC}"
RESPONSE=$(curl -s -X POST "$API_URL/repair/list" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"phone\":\"$TEST_PHONE\"}")

if echo "$RESPONSE" | grep -q "$TICKET_NUMBER"; then
    LIST_COUNT=$(echo "$RESPONSE" | grep -o '"count":[0-9]*' | cut -d':' -f2)
    echo -e "${GREEN}✓ 工单列表查询成功${NC}"
    echo "  该手机号名下返回 $LIST_COUNT 张工单，包含刚创建的 $TICKET_NUMBER"
else
    echo -e "${RED}✗ 工单列表未包含刚创建的工单${NC}"
    echo "$RESPONSE"
    exit 1
fi
echo ""

# 测试 7c: 工单列表 - 未知手机号返回空
echo -e "${YELLOW}测试 7c: 工单列表（未知手机号 → 空）${NC}"
RESPONSE=$(curl -s -X POST "$API_URL/repair/list" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"phone":"19900000000"}')

if echo "$RESPONSE" | grep -q '"count":0'; then
    echo -e "${GREEN}✓ 未知手机号正确返回 0 张工单${NC}"
else
    echo -e "${RED}✗ 未知手机号列表异常${NC}"
    echo "$RESPONSE"
fi
echo ""

# 测试8: 取消工单
echo -e "${YELLOW}测试 8: 取消工单${NC}"
JSON_DATA=$(printf '{"woNumber":"%s"}' "$TICKET_NUMBER")
RESPONSE=$(curl -s -X POST "$API_URL/repair/cancel" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "$JSON_DATA")

if echo "$RESPONSE" | grep -q "cancelled"; then
    echo -e "${GREEN}✓ 工单取消成功${NC}"
    echo "  工单号: $TICKET_NUMBER"
else
    echo -e "${RED}✗ 工单取消失败${NC}"
    echo "$RESPONSE"
fi
echo ""

# 测试9: 重复取消应返回 409
echo -e "${YELLOW}测试 9: 错误处理 - 重复取消${NC}"
RESPONSE=$(curl -s -X POST "$API_URL/repair/cancel" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "$JSON_DATA")

if echo "$RESPONSE" | grep -q "already"; then
    echo -e "${GREEN}✓ 错误处理正确${NC}"
else
    echo -e "${RED}✗ 错误处理异常${NC}"
    echo "$RESPONSE"
fi
echo ""

echo -e "${GREEN}=== 所有测试完成 ===${NC}"
echo ""
echo "测试工单号: $TICKET_NUMBER"
echo "可以在DynamoDB中查看该工单数据"
