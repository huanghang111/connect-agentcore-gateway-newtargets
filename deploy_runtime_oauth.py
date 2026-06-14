"""Create/update a SECOND AgentCore Runtime with Keycloak JWT inbound auth.

This is an ALTERNATIVE entry point to the MCP server, meant to be added to
Amazon Quick Suite DIRECTLY as an MCP server (no AgentCore Gateway in front).
Quick obtains an OAuth 2.0 access token from Keycloak and sends it as a Bearer
token; this runtime's inbound JWT authorizer validates the token against the
Keycloak OIDC discovery URL before allowing the call.

It is fully INDEPENDENT of the existing deploy.sh / deploy_runtime.py path:
  - The original runtime (`connect_repair_mcp_server`, SigV4 auth) and its
    AgentCore Gateway (authorizerType=NONE) are left completely untouched, so
    you can switch back to that architecture at any time.
  - This script reuses the SAME ECR image (same 4-tool MCP server code) and the
    SAME execution role, only differing in the inbound authorizer and the
    runtime name.

Configuration is read from the `.env` file in this directory. Required keys
(all already present from deploy.sh, plus the OAuth keys):
  REGION, ACCOUNT_ID, ECR_REPO_NAME, REPAIR_API_URL, REPAIR_API_KEY,
  OAUTH_DISCOVERY_URL    (Keycloak .well-known/openid-configuration URL)
  OAUTH_ALLOWED_AUDIENCE (JWT `aud` claim to validate; defaults to "account",
                          which is what makes the Amazon Quick connector work)
  OAUTH_CLIENT_ID        (the OAuth client_id Quick uses, e.g. amazon-quick-mcp-auth;
                          display-only — printed in the Quick config checklist)
  OAUTH_AGENT_NAME       (runtime name for this OAuth variant; underscores only)

SECURITY: this script needs only the (non-secret) discovery URL and client id.
The OAuth client SECRET is NEVER used here and must NOT be stored — AWS validates
the JWT via Keycloak's public JWKS; only the Quick client needs the secret to
mint tokens.
"""
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

import boto3


def _load_env(path: Path) -> None:
    if not path.exists():
        print(f"✗ {path} not found. Run deploy.sh once first (it creates .env), "
              f"then add the OAUTH_* keys.")
        sys.exit(1)
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"✗ {name} is required in .env")
        sys.exit(1)
    return val


_load_env(Path(__file__).parent / ".env")

REGION = _require("REGION")
ACCOUNT_ID = os.environ.get("ACCOUNT_ID", "").strip()
if not ACCOUNT_ID:
    # deploy.sh resolves this via STS at runtime rather than persisting it.
    ACCOUNT_ID = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    print(f"  Resolved ACCOUNT_ID via STS: {ACCOUNT_ID}")
ECR_REPO_NAME = _require("ECR_REPO_NAME")
REPAIR_API_URL = _require("REPAIR_API_URL")
REPAIR_API_KEY = _require("REPAIR_API_KEY")

OAUTH_DISCOVERY_URL = _require("OAUTH_DISCOVERY_URL")
# JWT audience to validate against the token's `aud` claim. Amazon Quick's
# 3LO tokens from Keycloak carry aud=["<client_id>","account"]; validating on
# the shared "account" audience is what makes the Quick connector publish
# succeed (validating on allowedClients instead caused "Creation failed").
OAUTH_ALLOWED_AUDIENCE = os.environ.get("OAUTH_ALLOWED_AUDIENCE", "account").strip()
# Display-only: the client id Quick uses to mint tokens (not used for validation).
OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", os.environ.get("OAUTH_ALLOWED_CLIENT", "")).strip()
# Runtime names must use underscores. Default keeps it distinct from the SigV4 one.
OAUTH_AGENT_NAME = os.environ.get("OAUTH_AGENT_NAME", "connect_repair_mcp_server_oauth").strip()

# Reuse the existing execution role created by deploy.sh for the SigV4 runtime.
# AGENT_NAME (dashed) is what deploy.sh used to name that role.
AGENT_NAME_DASH = os.environ.get("AGENT_NAME", "connect-repair-mcp-server").strip()
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/{AGENT_NAME_DASH}-execution-role"
ECR_URI = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO_NAME}"

# Optional response-normalization knobs (mirror deploy_runtime.py so behaviour matches).
_OPT = {}
for k in ("NORMALIZE_RESPONSE", "NORMALIZE_MODEL_ID", "BEDROCK_REGION", "NORMALIZE_TIMEOUT_S"):
    v = os.environ.get(k, "").strip()
    if v:
        _OPT[k] = v


def main():
    control = boto3.client("bedrock-agentcore-control", region_name=REGION)

    authorizer = {
        "customJWTAuthorizer": {
            "discoveryUrl": OAUTH_DISCOVERY_URL,
            "allowedAudience": [OAUTH_ALLOWED_AUDIENCE],
        }
    }

    runtime_params = dict(
        agentRuntimeName=OAUTH_AGENT_NAME,
        agentRuntimeArtifact={
            "containerConfiguration": {"containerUri": f"{ECR_URI}:latest"}
        },
        roleArn=ROLE_ARN,
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "MCP"},
        authorizerConfiguration=authorizer,
        lifecycleConfiguration={
            "idleRuntimeSessionTimeout": 3600,
            "maxLifetime": 28800,
        },
        environmentVariables={
            "REPAIR_API_URL": REPAIR_API_URL,
            "REPAIR_API_KEY": REPAIR_API_KEY,
            "OTEL_SERVICE_NAME": OAUTH_AGENT_NAME,
            **_OPT,
        },
    )

    print(f"=== OAuth (Keycloak JWT) Runtime — create/update ===")
    print(f"  Region:        {REGION}")
    print(f"  Runtime name:  {OAUTH_AGENT_NAME}")
    print(f"  Image:         {ECR_URI}:latest")
    print(f"  Exec role:     {ROLE_ARN}")
    print(f"  Discovery URL: {OAUTH_DISCOVERY_URL}")
    print(f"  Allowed audience: {OAUTH_ALLOWED_AUDIENCE}")
    print()

    # Find existing runtime with this name (idempotent re-run).
    runtime_id = None
    paginator = control.get_paginator("list_agent_runtimes")
    for page in paginator.paginate():
        for rt in page.get("agentRuntimes", []):
            if rt["agentRuntimeName"] == OAUTH_AGENT_NAME:
                runtime_id = rt["agentRuntimeId"]
                break
        if runtime_id:
            break

    if runtime_id:
        print(f"  Runtime exists: {runtime_id} — updating")
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
        if "FAILED" in status:
            print(f"  ✗ Failed: {status}")
            print(f"  Reason: {resp.get('failureReason', 'unknown')}")
            sys.exit(1)
        print(f"  Status: {status} ({i+1}/60)")
        time.sleep(10)
    else:
        print("  ✗ Timeout waiting for READY")
        sys.exit(1)

    encoded_arn = quote(runtime_arn, safe="")
    mcp_url = (
        f"https://bedrock-agentcore.{REGION}.amazonaws.com"
        f"/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    )

    print(f"\n{'='*60}")
    print("OAuth MCP Runtime deployed (add this DIRECTLY in Quick)")
    print(f"{'='*60}")
    print(f"Runtime ID:    {runtime_id}")
    print(f"Runtime ARN:   {runtime_arn}")
    print(f"Inbound auth:  Custom JWT (Keycloak)")
    print(f"MCP URL:       {mcp_url}")
    print()
    print("Quick MCP server config — paste these into Quick (User auth / Custom user based OAuth):")
    print(f"  MCP server URL : {mcp_url}")
    print(f"  Auth type      : OAuth 2.0 (User authentication / 3LO)")
    print(f"  Client id      : {OAUTH_CLIENT_ID or '<your OAuth client id>'}")
    print(f"  Client secret  : <the secret you hold — NOT stored here>")
    print(f"  Token URL      : (from Keycloak) .../protocol/openid-connect/token")
    print(f"  Authorization URL: (from Keycloak) .../protocol/openid-connect/auth")
    print(f"  Redirect URL   : (your Quick redirect, e.g. https://us-east-1.quicksight.aws.amazon.com/sn/oauthcallback)")

    with open(Path(__file__).parent / "deployment-info-oauth.log", "w") as f:
        f.write(f"Runtime ID:    {runtime_id}\n")
        f.write(f"Runtime ARN:   {runtime_arn}\n")
        f.write(f"Inbound auth:  Custom JWT (Keycloak)\n")
        f.write(f"Discovery URL: {OAUTH_DISCOVERY_URL}\n")
        f.write(f"Allowed audience: {OAUTH_ALLOWED_AUDIENCE}\n")
        f.write(f"Client id (Quick): {OAUTH_CLIENT_ID}\n")
        f.write(f"MCP URL:       {mcp_url}\n")
        f.write(f"ECR Image:     {ECR_URI}:latest\n")


if __name__ == "__main__":
    main()
