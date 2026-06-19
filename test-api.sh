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

echo -e "${YELLOW}=== Industrial Robot Repair Service API 测试 ===${NC}\n"
echo "API URL: $API_URL"
echo "API Key: ${API_KEY:0:10}..."
echo ""
echo "注: 身份核验已关闭 —— 任意调用方可创建/查询/修改/取消任意工单，无需 customerId。"
echo ""

# 测试1: 创建维修工单 (无需 customerId)
echo -e "${YELLOW}测试 1: 创建维修工单${NC}"
RESPONSE=$(curl -s -X POST "$API_URL/repair/request" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "productCategory": "仓储机器人",
    "productsubCategory": "导航传感器",
    "productModel": "WR-500 #7",
    "serialNumber": "SN20231015ABC123",
    "brand": "中科机器人",
    "description": "导航传感器异常，频繁偏航，需要现场标定。"
  }')

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

# 测试2: 查询工单状态 (无需 customerId)
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

# 测试3: FAQ查询 - 导航偏航
echo -e "${YELLOW}测试 3: FAQ查询 - 导航偏航${NC}"
RESPONSE=$(curl -s -X POST "$API_URL/faq/simple" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "仓储机器人频繁偏航怎么办"}')

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

# 测试4: FAQ查询 - 电池续航
echo -e "${YELLOW}测试 4: FAQ查询 - 电池续航${NC}"
RESPONSE=$(curl -s -X POST "$API_URL/faq/simple" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "机器人电池续航下降"}')

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
  -d '{"productCategory": "仓储机器人"}')

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
  --data-raw '{"woNumber":"WO-2026-9999"}')

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

if echo "$RESPONSE" | grep -q "WO-YYYY-NNNN"; then
    echo -e "${GREEN}✓ 错误处理正确${NC}"
else
    echo -e "${RED}✗ 错误处理异常${NC}"
    echo "$RESPONSE"
fi
echo ""

# 测试8: 取消工单 (无需 customerId)
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

# 测试10: 预置工单查询 (WO-2026-0001, 任意调用方均可查)
echo -e "${YELLOW}测试 10: 预置工单查询 (WO-2026-0001)${NC}"
RESPONSE=$(curl -s -X POST "$API_URL/repair/track" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  --data-raw '{"woNumber":"WO-2026-0001"}')

if echo "$RESPONSE" | grep -q "Repair ticket found"; then
    echo -e "${GREEN}✓ 预置工单查询成功${NC}"
else
    echo -e "${RED}✗ 预置工单查询失败${NC}"
    echo "$RESPONSE"
fi
echo ""

# 测试11: 更新工单 - 改优先级+状态 (无需 customerId, 任意调用方均可改)
echo -e "${YELLOW}测试 11: 更新工单 (优先级+状态)${NC}"
RESPONSE=$(curl -s -X POST "$API_URL/repair/update" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  --data-raw '{"woNumber":"WO-2026-0003","priority":"P0","status":"in_progress"}')

if echo "$RESPONSE" | grep -q "Repair ticket updated"; then
    echo -e "${GREEN}✓ 工单更新成功${NC}"
else
    echo -e "${RED}✗ 工单更新失败${NC}"
    echo "$RESPONSE"
fi
echo ""

# 测试12: 非法优先级应返回 400
echo -e "${YELLOW}测试 12: 错误处理 - 非法优先级${NC}"
RESPONSE=$(curl -s -X POST "$API_URL/repair/update" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  --data-raw '{"woNumber":"WO-2026-0003","priority":"P9"}')

if echo "$RESPONSE" | grep -q "priority must be one of"; then
    echo -e "${GREEN}✓ 错误处理正确（拒绝非法优先级）${NC}"
else
    echo -e "${RED}✗ 错误处理异常${NC}"
    echo "$RESPONSE"
fi
echo ""

# 测试13: 更新不存在的工单应返回 404
echo -e "${YELLOW}测试 13: 错误处理 - 更新不存在的工单${NC}"
RESPONSE=$(curl -s -X POST "$API_URL/repair/update" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  --data-raw '{"woNumber":"WO-2026-9999","status":"completed"}')

if echo "$RESPONSE" | grep -q "not found"; then
    echo -e "${GREEN}✓ 错误处理正确（工单不存在）${NC}"
else
    echo -e "${RED}✗ 错误处理异常${NC}"
    echo "$RESPONSE"
fi
echo ""

# 还原 WO-2026-0003 到预置状态
curl -s -X POST "$API_URL/repair/update" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  --data-raw '{"woNumber":"WO-2026-0003","priority":"P2","status":"pending"}' >/dev/null
echo "（已还原 WO-2026-0003 到预置状态 P2/pending）"
echo ""

echo -e "${GREEN}=== 所有测试完成 ===${NC}"
echo ""
echo "测试工单号: $TICKET_NUMBER"
echo "可以在DynamoDB中查看该工单数据"
