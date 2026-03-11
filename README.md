# Connect Repair Service API

简化的维修服务API，包含工单管理和FAQ查询功能。
方案架构图如下:
<img width="1046" height="417" alt="image" src="https://github.com/user-attachments/assets/cd0cc9c4-ef83-4de3-a27a-5a8a131f2356" />


## 快速部署

```bash
cd connect-agentcore-gateway-newtargets
./deploy.sh
```

部署完成后查看 `deployment-info.log` 获取API信息。

## 测试API

```bash
# 查看部署信息
cat deployment-info.log

# 设置环境变量
export API_URL="<your-api-url>"
export API_KEY="<your-api-key>"

# 运行测试
./test-api.sh
```

## 清理资源

```bash
./cleanup.sh
```

## API端点

### 1. POST /repair/request - 创建维修工单
```json
{
  "product_model": "产品型号",
  "serial_number": "序列号",
  "purchase_date": "2023-10-15",
  "issue_description": "问题描述",
  "full_name": "客户姓名",
  "phone": "+1-555-0123",
  "service_address": "服务地址",
  "preferred_time": "2024-02-15 10:00",
  "warranty_status": "yes"
}
```
返回：10位工单号

### 2. POST /repair/track - 查询工单状态
```json
{
  "repair_notice_or_work_order_number": "工单号",
  "full_name": "客户姓名",
  "phone": "+1-555-0123",
  "need_to_reschedule_or_missed_visit": "no",
  "waiting_for_spare_part": "no"
}
```
返回：工单详细信息

### 3. POST /faq/simple - FAQ查询
```json
{
  "query": "How do I reset my air conditioner?"
}
```
返回：相关FAQ列表（内置10条常见问题）

## 架构

```
API Gateway (API Key认证)
├── /repair/request  → Lambda → DynamoDB
├── /repair/track    → Lambda → DynamoDB
└── /faq/simple      → Lambda (内置FAQ)
```

**资源：** 1个DynamoDB表 + 3个Lambda函数

**成本：** ~$5/月 (低流量)

## 文件说明

- `connect-api-customer.yaml` - CloudFormation模板
- `connect-api-openapi.yaml` - OpenAPI规范
- `deploy.sh` - 自动部署脚本
- `test-api.sh` - API测试脚本
- `cleanup.sh` - 资源清理脚本
- `deployment-info.log` - 部署信息（部署后生成）

## 手动部署

如果不使用自动脚本：

```bash
# 1. 上传OpenAPI规范到S3
aws s3 cp connect-api-openapi.yaml s3://your-bucket/

# 2. 创建CloudFormation stack
aws cloudformation create-stack \
  --stack-name connect-ac-api-stack \
  --template-body file://connect-api-customer.yaml \
  --parameters ParameterKey=OpenApiSpecUrl,ParameterValue=s3://your-bucket/connect-api-openapi.yaml \
  --capabilities CAPABILITY_IAM \
  --region us-west-2

# 3. 等待完成
aws cloudformation wait stack-create-complete \
  --stack-name connect-ac-api-stack \
  --region us-west-2
```

## 测试用例

`test-api.sh` 包含6个测试：
1. ✓ 创建维修工单
2. ✓ 查询工单状态
3. ✓ FAQ查询 - 空调重置
4. ✓ FAQ查询 - 冰箱噪音
5. ✓ 错误处理 - 缺少参数
6. ✓ 错误处理 - 工单不存在

## 常见问题

**Q: 部署需要多久？**
3-5分钟

**Q: 如何查看日志？**
AWS Console → CloudWatch → Log groups → `/aws/lambda/connect-ac-api-stack-repair-api`

**Q: 成本会超出预算吗？**
使用AWS Free Tier，前12个月几乎免费。之后低流量场景约$5/月。

**Q: 如何修改API？**
修改对应的YAML文件后重新部署即可。
