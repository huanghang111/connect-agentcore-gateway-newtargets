"""Deploy AgentCore Runtime and add Gateway Target (Steps 6-8 of deploy.sh).

Configuration is loaded from the `.env` file in this directory.
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
AGENT_NAME_DASH = _require("AGENT_NAME")          # e.g. midea-repair-mcp-server
AGENT_NAME = AGENT_NAME_DASH.replace("-", "_")     # Runtime names must use underscores
ECR_REPO_NAME = _require("ECR_REPO_NAME")
GATEWAY_ID = _require("GATEWAY_ID")
GATEWAY_SERVICE_ROLE = _require("GATEWAY_SERVICE_ROLE")
TARGET_NAME = _require("TARGET_NAME")

REPAIR_API_URL = _require("REPAIR_API_URL")
REPAIR_API_KEY = _require("REPAIR_API_KEY")

ECR_URI = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO_NAME}"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/{AGENT_NAME_DASH}-execution-role"


def main():
    control = boto3.client("bedrock-agentcore-control", region_name=REGION)
    iam = boto3.client("iam")

    # Step 6: Create or Update Runtime
    print("Step 6/8: AgentCore Runtime (MCP)")
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

    # Wait for READY
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

    # Step 7: Ensure Gateway service role can invoke this runtime
    print("\nStep 7/8: Gateway IAM Permission")
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
        RoleName=GATEWAY_SERVICE_ROLE,
        PolicyName="InvokeAgentRuntimePolicy",
        PolicyDocument=policy_doc,
    )
    print("  ✓ InvokeAgentRuntime permission added")
    time.sleep(10)

    # Step 8: Gateway Target
    print("\nStep 8/8: Gateway Target (mcpServer)")
    encoded_arn = quote(runtime_arn, safe="")
    mcp_endpoint = (
        f"https://bedrock-agentcore.{REGION}.amazonaws.com"
        f"/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    )

    # Check existing targets
    targets_resp = control.list_gateway_targets(gatewayIdentifier=GATEWAY_ID)
    existing_target_id = None
    for t in targets_resp.get("items", []):
        if t["name"] == TARGET_NAME:
            existing_target_id = t["targetId"]
            break

    if existing_target_id:
        print(f"  Target exists: {existing_target_id}")
        print("  Synchronizing tools...")
        try:
            control.synchronize_gateway_targets(
                gatewayIdentifier=GATEWAY_ID,
                targetIdList=[existing_target_id],
            )
            print("  ✓ Sync triggered")
        except Exception as e:
            print(f"  Sync note: {e}")
    else:
        resp = control.create_gateway_target(
            gatewayIdentifier=GATEWAY_ID,
            name=TARGET_NAME,
            description="Midea Repair MCP Server on AgentCore Runtime",
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
                gatewayIdentifier=GATEWAY_ID, targetId=target_id
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

    # Summary
    print(f"\n{'='*50}")
    print("Deployment Complete!")
    print(f"{'='*50}")
    print(f"Agent Runtime ID:  {runtime_id}")
    print(f"Agent Runtime ARN: {runtime_arn}")
    print(f"MCP Endpoint:      {mcp_endpoint}")
    print(f"Gateway ID:        {GATEWAY_ID}")
    print(f"Target Name:       {TARGET_NAME}")
    print(f"\nMCP Tools: requestRepair, trackRepair, faqSearch")

    # Save info
    with open("deployment-info.log", "w") as f:
        f.write(f"=== MCP Agent Deployment Info ===\n")
        f.write(f"Region: {REGION}\n")
        f.write(f"Agent Runtime ID: {runtime_id}\n")
        f.write(f"Agent Runtime ARN: {runtime_arn}\n")
        f.write(f"ECR Image: {ECR_URI}:latest\n")
        f.write(f"MCP Endpoint: {mcp_endpoint}\n")
        f.write(f"Gateway ID: {GATEWAY_ID}\n")
        f.write(f"Target Name: {TARGET_NAME}\n")
        f.write(f"CodeBuild: {AGENT_NAME_DASH}-build\n")


if __name__ == "__main__":
    main()
