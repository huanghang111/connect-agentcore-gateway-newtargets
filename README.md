# Connect Service API - Complete Stack

Complete CloudFormation stack combining Hotel Reservation and Midea Repair Service APIs.

## What This Creates

**Hotel APIs:**
- Search hotels by city
- Create, modify, cancel reservations
- Get customer reservations

**Repair Service APIs:**
- Request repair service (generates 10-digit ticket number)
- Track repair ticket status
- FAQ search using Bedrock Knowledge Base
- FAQ simple search (predefined database, no Bedrock required)

**Infrastructure:**
- 3 DynamoDB tables (Hotels, Reservations, RepairTickets)
- 9 Lambda functions
- API Gateway with API Key authentication
- CloudWatch logging and X-Ray tracing

## Prerequisites

1. **Bedrock Knowledge Base** - Create manually before deployment (optional, only needed for /faq/search endpoint)
2. **Hotel seed data** - JSON file with hotel data (S3 or HTTPS URL)
3. **OpenAPI spec** - YAML file with API specification (S3 or HTTPS URL)
4. AWS CLI configured with appropriate permissions

**Note:** The `/faq/simple` endpoint works without Bedrock Knowledge Base and uses a predefined FAQ database.

## Quick Start

### 1. Create Bedrock Knowledge Base

```bash
# Create S3 bucket for FAQ documents
aws s3 mb s3://midea-repair-faq-docs-<account-id> --region us-east-1

# Upload FAQ documents
aws s3 cp your-faq.pdf s3://midea-repair-faq-docs-<account-id>/
aws s3 cp your-manual.txt s3://midea-repair-faq-docs-<account-id>/
```

Then create Knowledge Base via AWS Console:
- Go to Amazon Bedrock → Knowledge bases → Create
- Name: `midea-repair-faq-kb`
- Embeddings model: Titan Embeddings G1 - Text
- Data source: S3 bucket created above
- Vector store: Quick create new vector store
- Click Sync after creation

Get Knowledge Base ID:
```bash
aws bedrock-agent list-knowledge-bases --region us-east-1 \
  --query 'knowledgeBaseSummaries[?name==`midea-repair-faq-kb`].knowledgeBaseId' \
  --output text
```

### 2. Deploy Stack

```bash
aws cloudformation create-stack \
  --stack-name connect-service-api \
  --template-body file://connect-api-customer.yaml \
  --parameters \
    ParameterKey=SeedDataUrl,ParameterValue=<YOUR_HOTEL_DATA_URL> \
    ParameterKey=OpenApiSpecUrl,ParameterValue=<YOUR_OPENAPI_SPEC_URL> \
    ParameterKey=KnowledgeBaseId,ParameterValue=<YOUR_KB_ID> \
  --capabilities CAPABILITY_IAM \
  --region us-east-1
```

Wait for completion:
```bash
aws cloudformation wait stack-create-complete \
  --stack-name connect-service-api \
  --region us-east-1
```

### 3. Get API Details

```bash
# Get API URL
aws cloudformation describe-stacks \
  --stack-name connect-service-api \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiUrl`].OutputValue' \
  --output text

# Get API Key
aws cloudformation describe-stacks \
  --stack-name connect-service-api \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiKey`].OutputValue' \
  --output text
```

## API Usage Examples

Set environment variables:
```bash
export API_URL=<your-api-url>
export API_KEY=<your-api-key>
```

### Hotel APIs

**Search Hotels:**
```bash
curl -X POST $API_URL/hotels/search \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"city": "Seattle"}'
```

**Create Reservation:**
```bash
curl -X POST $API_URL/reservations \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "hotelId": "hotel-123",
    "customerId": "customer-456",
    "checkInDate": "2024-03-15",
    "checkOutDate": "2024-03-18",
    "roomType": "Standard King",
    "guestName": "John Doe",
    "guestEmail": "john@example.com"
  }'
```

### Repair Service APIs

**Request Repair:**
```bash
curl -X POST $API_URL/repair/request \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "product_model": "AC-2000X",
    "serial_number": "SN123456789",
    "purchase_date": "2023-06-15",
    "issue_description": "Not cooling properly",
    "full_name": "John Smith",
    "phone": "+1-555-123-4567",
    "service_address": "123 Main St, New York, NY 10001",
    "preferred_time": "2024-02-15 14:00",
    "warranty_status": "yes"
  }'
```

**Track Repair:**
```bash
curl -X POST $API_URL/repair/track \
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

**FAQ Search (Bedrock):**
```bash
curl -X POST $API_URL/faq/search \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "How do I reset my air conditioner?"}'
```

**FAQ Simple (No Bedrock):**
```bash
curl -X POST $API_URL/faq/simple \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "How do I reset my air conditioner?"}'
```

## Stack Outputs

- `ApiUrl` - Base URL for all API endpoints
- `ApiKey` - API key for authentication
- `HotelTableName` - DynamoDB table for hotels
- `ReservationsTableName` - DynamoDB table for reservations
- `RepairTicketsTableName` - DynamoDB table for repair tickets
- `OpenApiSpecS3Location` - S3 location of OpenAPI spec
- `ApiEndpoints` - List of all available endpoints

## Cleanup

```bash
# Delete CloudFormation stack
aws cloudformation delete-stack \
  --stack-name connect-service-api \
  --region us-east-1

# Delete Knowledge Base (manual via console or CLI)
aws bedrock-agent delete-knowledge-base \
  --knowledge-base-id <YOUR_KB_ID> \
  --region us-east-1

# Delete S3 bucket
aws s3 rb s3://midea-repair-faq-docs-<account-id> --force --region us-east-1
```

## Architecture

```
API Gateway (with API Key auth)
├── Hotel APIs
│   ├── /hotels/search → SearchHotelsFunction → HotelsTable
│   ├── /reservations → CreateReservationFunction → ReservationsTable
│   ├── /reservations/cancel → CancelReservationFunction → ReservationsTable
│   ├── /reservations/customer → GetCustomerReservationsFunction → ReservationsTable
│   └── /reservations/modify → ModifyReservationFunction → ReservationsTable
└── Repair APIs
    ├── /repair/request → RequestRepairFunction → RepairTicketsTable
    ├── /repair/track → TrackRepairFunction → RepairTicketsTable
    ├── /faq/search → FaqSearchFunction → Bedrock Knowledge Base
    └── /faq/simple → FaqSimpleFunction → Predefined FAQ Database (no external dependency)
```

## Cost Estimate

- Lambda: ~$0.20/month (1M requests)
- DynamoDB: ~$2/month (100K operations across 3 tables)
- API Gateway: ~$3.50/month (1M requests)
- Bedrock KB: ~$0.10 per 1K tokens
- CloudWatch: ~$0.50/month (1GB logs)

**Total: ~$6-7/month** (excluding Bedrock Knowledge Base infrastructure costs)

## Files

- `connect-api-customer.yaml` - Complete CloudFormation template
- `connect-api-openapi.yaml` - Complete OpenAPI specification
- `hotel-api-customer-addition.yaml` - Legacy addition file (reference only)
- `hotel-api-openapi-addition.yaml` - Legacy addition file (reference only)
- `openapi-addition.yaml` - Legacy addition file (reference only)
