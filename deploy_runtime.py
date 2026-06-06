"""Deploy AgentCore Runtime + Gateway + Target (steps 10-12 of deploy.sh).

Configuration is loaded from the `.env` file in the same directory as this
script. Called by the unified `deploy.sh` after the Backend API stack is
deployed and after the MCP Agent ARM64 image has been pushed to ECR.

The Gateway is created with ``authorizerType=NONE`` (open inbound) for
testing — anyone with the MCP URL can call the tools. To switch to a
production-grade Gateway, set ``GATEWAY_ID`` + ``GATEWAY_SERVICE_ROLE`` in
.env to a Gateway already configured with CUSTOM_JWT or AWS_IAM.
"""
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

import boto3


def _load_env(path: Path) -> None:
    if not path.exists():
        print(f"✗ {path} not found. Copy .env.example to .env and fill in your values.")
        sys.exit(1)
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"✗ {name} is required in .env")
        sys.exit(1)
    return val


_load_env(Path(__file__).parent / ".env")

REGION = _require("REGION")
ACCOUNT_ID = _require("ACCOUNT_ID")
AGENT_NAME_DASH = _require("AGENT_NAME")          # e.g. connect-repair-mcp-server
AGENT_NAME = AGENT_NAME_DASH.replace("-", "_")     # Runtime names must use underscores
ECR_REPO_NAME = _require("ECR_REPO_NAME")
# Gateway is optional. Empty GATEWAY_ID → script auto-creates one with
# authorizerType=NONE (testing only).
GATEWAY_ID = os.environ.get("GATEWAY_ID", "").strip()
GATEWAY_SERVICE_ROLE = os.environ.get("GATEWAY_SERVICE_ROLE", "").strip()
TARGET_NAME = _require("TARGET_NAME")

REPAIR_API_URL = _require("REPAIR_API_URL")
REPAIR_API_KEY = _require("REPAIR_API_KEY")

# Optional response-normalization knobs.
NORMALIZE_RESPONSE_ENV = os.environ.get("NORMALIZE_RESPONSE", "").strip()
NORMALIZE_MODEL_ID_ENV = os.environ.get("NORMALIZE_MODEL_ID", "").strip()
BEDROCK_REGION_ENV = os.environ.get("BEDROCK_REGION", "").strip()
NORMALIZE_TIMEOUT_S_ENV = os.environ.get("NORMALIZE_TIMEOUT_S", "").strip()

# Identity-token signing key — Runtime must keep a stable secret across replicas.
IDENTITY_TOKEN_SECRET_ENV = os.environ.get("IDENTITY_TOKEN_SECRET", "").strip()
IDENTITY_TOKEN_TTL_S_ENV = os.environ.get("IDENTITY_TOKEN_TTL_S", "").strip()

ECR_URI = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO_NAME}"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/{AGENT_NAME_DASH}-execution-role"

# Gateway name only accepts [0-9a-zA-Z-], no underscores; max 48 chars.
GATEWAY_NAME = f"{AGENT_NAME_DASH}-gw"[:48]
GATEWAY_ROLE_NAME = f"{AGENT_NAME_DASH}-gateway-role"


def _ensure_gateway_service_role(iam) -> str:
    """Create or return the IAM role the auto-provisioned Gateway uses."""
    try:
        role = iam.get_role(RoleName=GATEWAY_ROLE_NAME)
        return role["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        pass

    trust = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    })
    role_arn = iam.create_role(
        RoleName=GATEWAY_ROLE_NAME,
        AssumeRolePolicyDocument=trust,
        Description="Service role for Connect Repair MCP AgentCore Gateway",
    )["Role"]["Arn"]
    iam.put_role_policy(
        RoleName=GATEWAY_ROLE_NAME,
        PolicyName="gateway-default",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": "*",
            }],
        }),
    )
    print(f"  ✓ Gateway service role created: {GATEWAY_ROLE_NAME}")
    print("  Waiting 10s for IAM propagation...")
    time.sleep(10)
    return role_arn


def _ensure_gateway(control, iam) -> tuple[str, str]:
    """Return (gateway_id, gateway_service_role_name), creating if needed.

    Auto-created Gateway uses ``authorizerType=NONE`` — open inbound, intended
    for Quick Connect / Quick Web testing. Provide ``GATEWAY_ID`` +
    ``GATEWAY_SERVICE_ROLE`` in .env to reuse a hardened Gateway instead.
    """
    global GATEWAY_ID, GATEWAY_SERVICE_ROLE

    if GATEWAY_ID:
        if not GATEWAY_SERVICE_ROLE:
            print("✗ GATEWAY_SERVICE_ROLE is required when GATEWAY_ID is set")
            sys.exit(1)
        print(f"  Using existing Gateway: {GATEWAY_ID}")
        return GATEWAY_ID, GATEWAY_SERVICE_ROLE

    paginator = control.get_paginator("list_gateways")
    for page in paginator.paginate():
        for gw in page.get("items", []):
            if gw.get("name") == GATEWAY_NAME:
                gw_id = gw["gatewayId"]
                print(f"  Found existing auto-created Gateway: {gw_id}")
                role_name = (GATEWAY_SERVICE_ROLE or GATEWAY_ROLE_NAME)
                return gw_id, role_name

    role_arn = _ensure_gateway_service_role(iam)

    print(f"  Creating Gateway: {GATEWAY_NAME} (authorizerType=NONE — testing only)")
    resp = control.create_gateway(
        name=GATEWAY_NAME,
        description=f"Auto-created Gateway for {AGENT_NAME_DASH} (inbound auth disabled)",
        roleArn=role_arn,
        protocolType="MCP",
        authorizerType="NONE",
    )
    gw_id = resp["gatewayId"]
    print(f"  ✓ Gateway created: {gw_id}")
    return gw_id, GATEWAY_ROLE_NAME


def main():
    control = boto3.client("bedrock-agentcore-control", region_name=REGION)
    iam = boto3.client("iam")

    # Step 10: Create or update Runtime
    print("Step 10/12: AgentCore Runtime (MCP)")
    runtime_id = None

    paginator = control.get_paginator("list_agent_runtimes")
    for page in paginator.paginate():
        for rt in page.get("agentRuntimes", []):
            if rt["agentRuntimeName"] == AGENT_NAME:
                runtime_id = rt["agentRuntimeId"]
                break
        if runtime_id:
            break

    runtime_params = dict(
        agentRuntimeName=AGENT_NAME,
        agentRuntimeArtifact={
            "containerConfiguration": {"containerUri": f"{ECR_URI}:latest"}
        },
        roleArn=ROLE_ARN,
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={
            "serverProtocol": "MCP",
        },
        lifecycleConfiguration={
            "idleRuntimeSessionTimeout": 3600,
            "maxLifetime": 28800,
        },
        environmentVariables={
            "REPAIR_API_URL": REPAIR_API_URL,
            "REPAIR_API_KEY": REPAIR_API_KEY,
            # OTEL service.name — used by Strands/ADOT to tag spans in CloudWatch
            # GenAI Observability. Other OTEL_EXPORTER_* vars are injected by
            # AgentCore Runtime when Tracing is enabled on the runtime.
            "OTEL_SERVICE_NAME": AGENT_NAME,
            **({"NORMALIZE_RESPONSE": NORMALIZE_RESPONSE_ENV} if NORMALIZE_RESPONSE_ENV else {}),
            **({"NORMALIZE_MODEL_ID": NORMALIZE_MODEL_ID_ENV} if NORMALIZE_MODEL_ID_ENV else {}),
            **({"BEDROCK_REGION": BEDROCK_REGION_ENV} if BEDROCK_REGION_ENV else {}),
            **({"NORMALIZE_TIMEOUT_S": NORMALIZE_TIMEOUT_S_ENV} if NORMALIZE_TIMEOUT_S_ENV else {}),
            **({"IDENTITY_TOKEN_SECRET": IDENTITY_TOKEN_SECRET_ENV} if IDENTITY_TOKEN_SECRET_ENV else {}),
            **({"IDENTITY_TOKEN_TTL_S": IDENTITY_TOKEN_TTL_S_ENV} if IDENTITY_TOKEN_TTL_S_ENV else {}),
        },
    )

    if runtime_id:
        print(f"  Runtime exists: {runtime_id}")
        update_params = {k: v for k, v in runtime_params.items() if k != "agentRuntimeName"}
        control.update_agent_runtime(agentRuntimeId=runtime_id, **update_params)
        print("  ✓ Updated")
    else:
        resp = control.create_agent_runtime(**runtime_params)
        runtime_id = resp["agentRuntimeId"]
        print(f"  ✓ Created: {runtime_id}")

    resp = control.get_agent_runtime(agentRuntimeId=runtime_id)
    runtime_arn = resp["agentRuntimeArn"]

    print("  Waiting for READY...")
    for i in range(60):
        resp = control.get_agent_runtime(agentRuntimeId=runtime_id)
        status = resp["status"]
        if status == "READY":
            print("  ✓ Runtime READY")
            break
        elif "FAILED" in status:
            print(f"  ✗ Failed: {status}")
            print(f"  Reason: {resp.get('failureReason', 'unknown')}")
            sys.exit(1)
        print(f"  Status: {status} ({i+1}/60)")
        time.sleep(10)
    else:
        print("  ✗ Timeout waiting for READY")
        sys.exit(1)

    # Step 11: Ensure Gateway and grant Gateway role InvokeAgentRuntime
    print("\nStep 11/12: AgentCore Gateway")
    gateway_id, gateway_service_role = _ensure_gateway(control, iam)

    policy_doc = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AllowInvokeAgentRuntime",
            "Effect": "Allow",
            "Action": "bedrock-agentcore:InvokeAgentRuntime",
            "Resource": [runtime_arn, f"{runtime_arn}/*"],
        }],
    })
    iam.put_role_policy(
        RoleName=gateway_service_role,
        PolicyName="InvokeAgentRuntimePolicy",
        PolicyDocument=policy_doc,
    )
    print("  ✓ InvokeAgentRuntime granted to Gateway role")
    time.sleep(10)

    # Step 12: Gateway Target (mcpServer)
    print("\nStep 12/12: Gateway Target")
    encoded_arn = quote(runtime_arn, safe="")
    mcp_endpoint = (
        f"https://bedrock-agentcore.{REGION}.amazonaws.com"
        f"/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    )

    targets_resp = control.list_gateway_targets(gatewayIdentifier=gateway_id)
    existing_target_id = None
    for t in targets_resp.get("items", []):
        if t["name"] == TARGET_NAME:
            existing_target_id = t["targetId"]
            break

    if existing_target_id:
        print(f"  Target exists: {existing_target_id}")
        try:
            control.synchronize_gateway_targets(
                gatewayIdentifier=gateway_id,
                targetIdList=[existing_target_id],
            )
            print("  ✓ Sync triggered")
        except Exception as e:
            print(f"  Sync note: {e}")
    else:
        resp = control.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=TARGET_NAME,
            description="Connect Repair MCP Server on AgentCore Runtime",
            targetConfiguration={
                "mcp": {"mcpServer": {"endpoint": mcp_endpoint}}
            },
            credentialProviderConfigurations=[
                {
                    "credentialProviderType": "GATEWAY_IAM_ROLE",
                    "credentialProvider": {
                        "iamCredentialProvider": {
                            "service": "bedrock-agentcore",
                            "region": REGION,
                        }
                    },
                }
            ],
        )
        target_id = resp["targetId"]
        print(f"  ✓ Target created: {target_id}")
        print("  Waiting for READY...")
        for i in range(30):
            t_resp = control.get_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target_id
            )
            t_status = t_resp["status"]
            if t_status == "READY":
                print("  ✓ Target READY")
                break
            elif t_status == "FAILED":
                print(f"  ✗ Target failed: {t_resp.get('statusReasons', [])}")
                sys.exit(1)
            print(f"  Status: {t_status} ({i+1}/30)")
            time.sleep(10)
        else:
            print("  ✗ Timeout waiting for target READY")
            sys.exit(1)

    # Resolve the public Gateway URL so users can wire it into Quick Connect / Quick Web.
    gateway_url = control.get_gateway(gatewayIdentifier=gateway_id).get("gatewayUrl", "")

    print(f"\n{'='*50}")
    print("MCP Agent deployed")
    print(f"{'='*50}")
    print(f"Runtime ID:        {runtime_id}")
    print(f"Runtime ARN:       {runtime_arn}")
    print(f"MCP Endpoint:      {mcp_endpoint}")
    print(f"Gateway ID:        {gateway_id}")
    print(f"Gateway URL:       {gateway_url}")
    print(f"Gateway authorizer: NONE (testing — open inbound)")
    print(f"Gateway Role:      {gateway_service_role}")
    print(f"Target Name:       {TARGET_NAME}")
    print(f"\nMCP Tools: verifyCustomer, verifyCustomerByPhoneAndName, "
          f"requestRepair, trackRepair, cancelRepair, faqSearch")

    with open("deployment-info-runtime.log", "w") as f:
        f.write(f"Runtime ID:        {runtime_id}\n")
        f.write(f"Runtime ARN:       {runtime_arn}\n")
        f.write(f"ECR Image:         {ECR_URI}:latest\n")
        f.write(f"MCP Endpoint:      {mcp_endpoint}\n")
        f.write(f"Gateway ID:        {gateway_id}\n")
        f.write(f"Gateway URL:       {gateway_url}\n")
        f.write(f"Gateway authorizer: NONE (testing — open inbound)\n")
        f.write(f"Gateway Role:      {gateway_service_role}\n")
        f.write(f"Target Name:       {TARGET_NAME}\n")
        f.write(f"CodeBuild Project: {AGENT_NAME_DASH}-build\n")


if __name__ == "__main__":
    main()
