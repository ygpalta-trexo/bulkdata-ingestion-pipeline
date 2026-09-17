"""
aws_secrets.py — database credentials from AWS Secrets Manager.

Ported from the DRM scripts (~/repos/scripts/drm/aws_secrets.py) so both
code-bases read the same secret shape, switched by the same env vars:

    USE_SECRET_MANAGER  — "true" to read credentials from the secret (default off)
    DB_SECRET_NAME      — secret name or ARN
    AWS_REGION          — region the secret lives in

docdb_ingestion.database.get_dsn_from_env() is the only caller. AWS credentials
come from boto3's default chain. On EC2 that should be the instance role, so leave
AWS keys out of .env there: get_dsn_from_env() loads .env into the environment
first, and environment keys take precedence over the instance role. The role
needs secretsmanager:GetSecretValue on the secret, plus kms:Decrypt if the
secret is encrypted with a customer-managed KMS key.

Errors name the secret but never carry its contents.
"""

import json
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)


def get_secret(secret_name: str, region_name: str) -> dict:
    """Return the secret's JSON object, cached for the life of the process.

    The pipeline resolves its DSN once per delivery, so without the cache a
    catch-up run would call AWS dozens of times. clear_cache() forces the next
    call to fetch again. A failed fetch raises and is not cached, so the next
    call retries. Callers get a copy, so mutating the result cannot change what
    later callers see.
    """
    if not secret_name:
        raise RuntimeError("DB_SECRET_NAME is not set")
    if not region_name:
        raise RuntimeError("AWS_REGION is not set")
    return dict(_fetch_secret(secret_name, region_name))


def clear_cache() -> None:
    """Forget every fetched secret, so the next get_secret() asks AWS again.

    DatabaseManager.connect() calls this when Postgres rejects credentials that
    came from the secret, which is how a rotation during a long run shows up.
    """
    _fetch_secret.cache_clear()


@lru_cache(maxsize=None)
def _fetch_secret(secret_name: str, region_name: str) -> dict:
    # boto3 is imported here rather than at module level: with the flag off
    # (local dev, tests) importing docdb_ingestion.database must not need it.
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    client = boto3.client("secretsmanager", region_name=region_name)
    try:
        response = client.get_secret_value(SecretId=secret_name)
    except (ClientError, BotoCoreError) as error:
        # BotoCoreError covers missing AWS credentials and unreachable
        # endpoints, which catching ClientError alone would let escape raw.
        raise RuntimeError(f"Unable to retrieve AWS secret '{secret_name}': {error}") from error

    secret_string = response.get("SecretString")
    if not secret_string:
        raise RuntimeError(f"AWS secret '{secret_name}' does not contain SecretString")

    # Raised outside the except block on purpose: json's parser error keeps the
    # whole secret text in .doc, and must not ride along as __context__ into
    # logs or error reporters.
    try:
        secret = json.loads(secret_string)
        parsed = True
    except json.JSONDecodeError:
        parsed = False
    if not parsed:
        raise RuntimeError(f"AWS secret '{secret_name}' does not contain valid JSON")

    if not isinstance(secret, dict):
        raise RuntimeError(f"AWS secret '{secret_name}' must contain a JSON object")

    logger.info(f"Loaded database credentials from AWS secret '{secret_name}' ({region_name})")
    return secret
