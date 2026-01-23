# Midea Repair Service API

CloudFormation stack that adds repair service APIs to your existing hotel API Gateway.

## What This Creates

- **3 New APIs**: Request repair, track repair, FAQ search
- **DynamoDB Table**: Stores repair tickets
- **Lambda Functions**: Backend for each API

## Prerequisites

1. Existing `hotel-api-stack` deployed in us-east-1
2. AWS CLI configured
3. **Bedrock Knowledge Base** created manually (see below)

## Step 1: Create Bedrock Knowledge Base

Before deploying the CloudFormation stack, create a Bedrock Knowledge Base manually:

### 1.1 Create S3 Bucket for Documents

```bash
aws s3 mb s3://midea-repair-faq-docs-585306731051 --region us-east-1
```

### 1.2 Upload FAQ Documents

```bash
aws s3 cp your-faq.pdf s3://midea-repair-faq-docs-585306731051/
aws s3 cp your-manual.txt s3://midea-repair-faq-docs-585306731051/
```

### 1.3 Create Knowledge Base via AWS Console

1. Go to **Amazon Bedrock** console → **Knowledge bases**
2. Click **Create knowledge base**
3. Configure:
   - **Name**: `midea-repair-faq-kb`
   - **IAM role**: Create new service role
   - **Embeddings model**: Titan Embeddings G1 - Text
4. **Data source**:
   - **S3 URI**: `s3://midea-repair-faq-docs-585306731051/`
5. **Vector store**: Choose **Quick create a new vector store**
6. Click **Create**
7. Wait for creation to complete
8. Click **Sync** to ingest documents

### 1.4 Get Knowledge Base ID

```bash
aws bedrock-agent list-knowledge-bases --region us-east-1 \
  --query 'knowledgeBaseSummaries[?name==`midea-repair-faq-kb`].knowledgeBaseId' \
  --output text
```
Response = XKZGTAUSXB

Save this ID - you'll need it for deployment.

## Step 2: Deploy CloudFormation Stack

### 2.1 Get API Gateway ID

```bash
aws cloudformation describe-stack-resources \
  --stack-name hotel-api-stack \
  --region us-east-1 \
  --logical-resource-id HotelApi \
  --query 'StackResources[0].PhysicalResourceId' \
  --output text
```
Response = lhmf5px1uh

### 2.2 Deploy the Stack

```bash
aws cloudformation create-stack \
  --stack-name midea-repair-api-stack \
  --template-body file://hotel-api-customer-addition.yaml \
  --parameters \
    ParameterKey=ExistingApiGatewayId,ParameterValue=YOUR_API_GATEWAY_ID \
    ParameterKey=KnowledgeBaseId,ParameterValue=YOUR_KNOWLEDGEBASE_ID \
  --capabilities CAPABILITY_IAM \
  --region us-east-1
```

### 2.3 Wait for Completion

```bash
aws cloudformation wait stack-create-complete \
  --stack-name midea-repair-api-stack \
  --region us-east-1
```

### 2.4 Deploy API Gateway Changes

After the stack is created, you need to manually deploy the API Gateway to activate the new endpoints:

```bash
aws apigateway create-deployment \
  --rest-api-id YOUR_API_GATEWAY_ID \
  --stage-name dev \
  --region us-east-1
```

Or use this one-liner:

```bash
API_ID=$(aws cloudformation describe-stack-resources \
  --stack-name hotel-api-stack \
  --region us-east-1 \
  --logical-resource-id HotelApi \
  --query 'StackResources[0].PhysicalResourceId' \
  --output text)

aws apigateway create-deployment \
  --rest-api-id $API_ID \
  --stage-name dev \
  --region us-east-1

echo "API Gateway deployed successfully!"
```

## API Usage

### Get API Key

```bash
API_KEY=$(aws cloudformation describe-stacks \
  --stack-name hotel-api-stack \
  --region us-east-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiKey`].OutputValue' \
  --output text)
```

### Get API Gateway ID

```bash
API_ID=$(aws cloudformation describe-stack-resources \
  --stack-name hotel-api-stack \
  --region us-east-1 \
  --logical-resource-id HotelApi \
  --query 'StackResources[0].PhysicalResourceId' \
  --output text)
```

### 1. Request Repair

```bash
curl -X POST https://$API_ID.execute-api.us-east-1.amazonaws.com/dev/repair/request \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "product_model": "AC-2000X",
    "serial_number": "SN123456789",
    "purchase_date": "2023-06-15",
    "issue_description": "Air conditioner not cooling properly",
    "full_name": "John Smith",
    "phone": "+1-555-123-4567",
    "service_address": "123 Main St, New York, NY 10001",
    "preferred_time": "2024-02-15 14:00",
    "warranty_status": "yes"
  }'
```

**Response:**
```json
{
  "message": "Repair ticket created successfully",
  "ticketNumber": "1234567890",
  "ticket": { ... }
}
```

### 2. Track Repair

```bash
curl -X POST https://$API_ID.execute-api.us-east-1.amazonaws.com/dev/repair/track \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "repair_notice_or_work_order_number": "1234567890",
    "full_name": "John Smith",
    "phone": "+1-555-123-4567",
    "need_to_reschedule_or_missed_visit": "no",
    "waiting_for_spare_part": "no"
  }'
```

**Response:**
```json
{
  "message": "Repair ticket found",
  "ticket": {
    "ticketNumber": "1234567890",
    "status": "pending",
    ...
  }
}
```

### 3. FAQ Search

```bash
curl -X POST https://$API_ID.execute-api.us-east-1.amazonaws.com/dev/faq/search \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "How do I reset my air conditioner?"
  }'
```

**Response:**
```json
{
  "query": "How do I reset my air conditioner?",
  "results": [
    {
      "content": "To reset your air conditioner...",
      "score": 0.85
    }
  ],
  "count": 1
}
```

## API Reference

### POST /repair/request
Creates a repair ticket and returns a 10-digit ticket number.

**Required Fields:**
- `product_model`, `serial_number`, `purchase_date`, `issue_description`
- `full_name`, `phone`, `service_address`, `preferred_time`, `warranty_status`

### POST /repair/track
Tracks repair ticket status. Requires customer verification.

**Required Fields:**
- `repair_notice_or_work_order_number`, `full_name`, `phone`
- `need_to_reschedule_or_missed_visit`, `waiting_for_spare_part`

### POST /faq/search
Searches the knowledge base using natural language.

**Required Fields:**
- `query`

## Troubleshooting

### API Returns 403 Forbidden (After Stack Creation)
If you get 403 errors immediately after creating the stack, you need to deploy the API Gateway:

```bash
API_ID=$(aws cloudformation describe-stack-resources \
  --stack-name hotel-api-stack \
  --region us-east-1 \
  --logical-resource-id HotelApi \
  --query 'StackResources[0].PhysicalResourceId' \
  --output text)

aws apigateway create-deployment \
  --rest-api-id $API_ID \
  --stage-name dev \
  --region us-east-1
```

### Stack Creation Fails
- Verify API Gateway ID is correct
- Check Knowledge Base ID is valid
- Ensure you have CAPABILITY_IAM permissions

### FAQ Search Returns No Results
- Verify Knowledge Base has been synced
- Check documents are uploaded to S3
- Wait a few minutes after sync completes

### API Returns 403 Forbidden
- Verify using correct API Key from hotel-api-stack
- Check API Key header: `X-API-Key`

## Cleanup

```bash
# Delete CloudFormation stack
aws cloudformation delete-stack \
  --stack-name midea-repair-api-stack \
  --region us-east-1

# Delete Knowledge Base (via console or CLI)
aws bedrock-agent delete-knowledge-base \
  --knowledge-base-id YOUR_KB_ID \
  --region us-east-1

# Delete S3 bucket
aws s3 rb s3://midea-repair-faq-docs-585306731051 --force --region us-east-1
```

## Architecture

```
Existing API Gateway (hotel-api-stack)
    │
    ├─► /repair/request  → RequestRepairFunction → DynamoDB
    ├─► /repair/track    → TrackRepairFunction   → DynamoDB
    └─► /faq/search      → FaqSearchFunction     → Bedrock KB (pre-created)
```

## Cost Estimate

- Lambda: ~$0.20/month (1M requests)
- DynamoDB: ~$1.25/month (100K operations)
- Bedrock KB: ~$0.10 per 1K tokens
- CloudWatch: ~$0.50/month (1GB logs)

**Total: ~$2/month** (excluding Bedrock Knowledge Base infrastructure costs)
