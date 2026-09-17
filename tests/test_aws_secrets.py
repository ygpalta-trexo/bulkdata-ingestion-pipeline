"""AWS Secrets Manager credential resolution. No real AWS calls, no real .env."""
import importlib
import json
import os
import subprocess
import sys
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import psycopg  # noqa: E402
from psycopg.conninfo import conninfo_to_dict  # noqa: E402

from docdb_ingestion import aws_secrets, database  # noqa: E402

SECRET_NAME = "prod/docdb/postgres"
REGION = "eu-central-1"


class FakeClientError(Exception):
    """Stands in for botocore.exceptions.ClientError."""


class FakeBotoCoreError(Exception):
    """Stands in for botocore.exceptions.BotoCoreError, e.g. no AWS credentials."""


class IsolatedTestCase(unittest.TestCase):
    """Controlled environment, fake AWS modules, empty credential cache."""

    BASE_ENV: dict = {}

    def setUp(self):
        # get_dsn_from_env() calls load_dotenv(override=True). Unpatched, that
        # would load the developer's real .env into the test process.
        self._patch(mock.patch.object(database, "load_dotenv", lambda *a, **k: False))
        self._patch(mock.patch.dict(os.environ, self.BASE_ENV, clear=True))
        aws_secrets._fetch_secret.cache_clear()
        self.addCleanup(aws_secrets._fetch_secret.cache_clear)
        database._SECRET_DSNS.clear()
        self.addCleanup(database._SECRET_DSNS.clear)
        self.clients = []   # (service, region) for each boto3.client() call
        self.fetches = []   # SecretId for each get_secret_value() call

    def _patch(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_aws(self, secret=None, secret_string=None, error=None):
        """Install fake boto3/botocore modules that serve one secret, or raise.

        A `secret` dict is serialized on every fetch, so a test can change it
        to simulate a rotation.
        """
        test = self

        class Client:
            def get_secret_value(self, SecretId):
                test.fetches.append(SecretId)
                if error is not None:
                    raise error
                body = json.dumps(secret) if secret is not None else secret_string
                return {} if body is None else {"SecretString": body}

        def client(service, region_name=None):
            test.clients.append((service, region_name))
            return Client()

        boto3 = types.ModuleType("boto3")
        boto3.client = client
        exceptions = types.ModuleType("botocore.exceptions")
        exceptions.ClientError = FakeClientError
        exceptions.BotoCoreError = FakeBotoCoreError
        botocore = types.ModuleType("botocore")
        botocore.exceptions = exceptions
        self._patch(mock.patch.dict(
            sys.modules, {"boto3": boto3, "botocore": botocore, "botocore.exceptions": exceptions}
        ))


class TestSecretManagerOff(IsolatedTestCase):
    BASE_ENV = {
        "POSTGRES_USER": "app",
        "POSTGRES_PASSWORD": "pw",
        "POSTGRES_HOST": "db.local",
        "POSTGRES_PORT": "5433",
        "POSTGRES_DB": "bulk-data",
    }

    def test_default_builds_the_same_dsn_as_before(self):
        self.fake_aws(secret={"username": "must-not-be-used"})
        self.assertEqual(database.get_dsn_from_env(), "postgresql://app:pw@db.local:5433/bulk-data")
        self.assertEqual(self.clients, [], "AWS must not be touched with the flag off")

    def test_database_url_fallback_is_unchanged(self):
        del os.environ["POSTGRES_DB"]
        os.environ["DATABASE_URL"] = "postgresql://u:p@elsewhere:5432/other"
        self.assertEqual(database.get_dsn_from_env(), "postgresql://u:p@elsewhere:5432/other")

    def test_only_true_turns_it_on(self):
        """Parsed like the DRM scripts, so one .env value means the same in both."""
        self.fake_aws(secret={"username": "s", "password": "s", "host": "rds.internal"})
        os.environ.update({"DB_SECRET_NAME": SECRET_NAME, "AWS_REGION": REGION})
        for value in ("false", "", "1", "yes", "on"):
            with self.subTest(value=value):
                os.environ["USE_SECRET_MANAGER"] = value
                self.assertIn("@db.local:", database.get_dsn_from_env())
        for value in ("true", "TRUE", "True"):
            with self.subTest(value=value):
                os.environ["USE_SECRET_MANAGER"] = value
                self.assertIn("@rds.internal:", database.get_dsn_from_env())

    def test_importing_the_database_module_does_not_need_boto3(self):
        """Local dev and CI run with the flag off and may not have boto3 installed."""
        result = subprocess.run(
            [sys.executable, "-c", "import sys, docdb_ingestion.database; print('boto3' in sys.modules)"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.stdout.strip(), "False", result.stderr)


class TestSecretManagerOn(IsolatedTestCase):
    BASE_ENV = {
        "USE_SECRET_MANAGER": "true",
        "DB_SECRET_NAME": SECRET_NAME,
        "AWS_REGION": REGION,
        "POSTGRES_DB": "bulk-data",
        # Present on purpose: with the flag on, all of these must be ignored.
        "POSTGRES_USER": "env_user",
        "POSTGRES_PASSWORD": "env_pw",
        "POSTGRES_HOST": "env-host",
        "DATABASE_URL": "postgresql://stray:stray@stray-host:5432/stray_db",
    }

    def test_rds_style_keys_with_database_name_from_env(self):
        self.fake_aws(secret={
            "username": "app", "password": "s3cret", "host": "rds.internal",
            "port": 5432, "dbname": "not_used",
        })
        parts = conninfo_to_dict(database.get_dsn_from_env())
        self.assertEqual(
            (parts["user"], parts["password"], parts["host"], parts["port"], parts["dbname"]),
            ("app", "s3cret", "rds.internal", "5432", "bulk-data"),
        )
        self.assertEqual(self.clients, [("secretsmanager", REGION)])
        self.assertEqual(self.fetches, [SECRET_NAME])

    def test_uppercase_keys(self):
        self.fake_aws(secret={
            "DB_USER": "app", "DB_PASSWORD": "s3cret", "DB_HOST": "rds.internal", "DB_PORT": "6543",
        })
        parts = conninfo_to_dict(database.get_dsn_from_env())
        self.assertEqual((parts["user"], parts["host"], parts["port"]), ("app", "rds.internal", "6543"))

    def test_env_credentials_and_database_url_are_ignored(self):
        self.fake_aws(secret={"username": "app", "password": "s3cret", "host": "rds.internal"})
        dsn = database.get_dsn_from_env()
        for ignored in ("env_user", "env_pw", "env-host", "stray"):
            self.assertNotIn(ignored, dsn)

    def test_special_character_credentials_survive_the_url(self):
        for password in ("p@ss:w/rd#1?", "has%percent", "with space", "plus+sign", "semi;colon&amp"):
            with self.subTest(password=password):
                aws_secrets._fetch_secret.cache_clear()
                self.fake_aws(secret={"username": "app user", "password": password, "host": "rds.internal"})
                parts = conninfo_to_dict(database.get_dsn_from_env())
                self.assertEqual(parts["password"], password)
                self.assertEqual(parts["user"], "app user")
                self.assertEqual(parts["host"], "rds.internal")
                self.assertEqual(parts["dbname"], "bulk-data")

    def test_port_falls_back_to_postgres_port_then_5432(self):
        self.fake_aws(secret={"username": "app", "password": "s3cret", "host": "rds.internal"})
        os.environ["POSTGRES_PORT"] = "6000"
        self.assertEqual(conninfo_to_dict(database.get_dsn_from_env())["port"], "6000")
        del os.environ["POSTGRES_PORT"]
        self.assertEqual(conninfo_to_dict(database.get_dsn_from_env())["port"], "5432")

    def test_invalid_port_is_a_clear_error(self):
        self.fake_aws(secret={"username": "app", "password": "s3cret", "host": "rds.internal", "port": "not-a-port"})
        with self.assertRaisesRegex(RuntimeError, "Invalid database port"):
            database.get_dsn_from_env()

    def test_missing_postgres_db_fails_before_calling_aws(self):
        del os.environ["POSTGRES_DB"]
        self.fake_aws(secret={"username": "app", "password": "s3cret", "host": "rds.internal"})
        with self.assertRaisesRegex(RuntimeError, "POSTGRES_DB"):
            database.get_dsn_from_env()
        self.assertEqual(self.clients, [])

    def test_missing_fields_are_named_without_leaking_values(self):
        self.fake_aws(secret={"username": "app", "host": "rds.internal"})
        with self.assertRaises(RuntimeError) as ctx:
            database.get_dsn_from_env()
        message = str(ctx.exception)
        self.assertIn("password", message)
        self.assertIn(SECRET_NAME, message)
        self.assertNotIn("rds.internal", message)

    def test_fetched_once_per_process(self):
        self.fake_aws(secret={"username": "app", "password": "s3cret", "host": "rds.internal"})
        self.assertEqual(database.get_dsn_from_env(), database.get_dsn_from_env())
        self.assertEqual(len(self.fetches), 1)


class TestGetSecret(IsolatedTestCase):
    def test_requires_name_and_region(self):
        with self.assertRaisesRegex(RuntimeError, "DB_SECRET_NAME"):
            aws_secrets.get_secret("", REGION)
        with self.assertRaisesRegex(RuntimeError, "AWS_REGION"):
            aws_secrets.get_secret(SECRET_NAME, None)

    def test_aws_errors_become_runtime_errors_naming_the_secret(self):
        for error in (FakeClientError("AccessDeniedException: not authorized"),
                      FakeBotoCoreError("Unable to locate credentials")):
            with self.subTest(error=type(error).__name__):
                aws_secrets._fetch_secret.cache_clear()
                self.fake_aws(error=error)
                with self.assertRaises(RuntimeError) as ctx:
                    aws_secrets.get_secret(SECRET_NAME, REGION)
                self.assertIn(SECRET_NAME, str(ctx.exception))
                self.assertIn(str(error), str(ctx.exception))

    def test_rejects_secrets_that_are_not_a_json_object(self):
        cases = [(None, "SecretString"), ("not json {", "valid JSON"), ("[1, 2]", "JSON object")]
        for secret_string, expected in cases:
            with self.subTest(secret_string=secret_string):
                aws_secrets._fetch_secret.cache_clear()
                self.fake_aws(secret_string=secret_string)
                with self.assertRaisesRegex(RuntimeError, expected):
                    aws_secrets.get_secret(SECRET_NAME, REGION)

    def test_invalid_json_error_does_not_carry_the_secret_text(self):
        """json's parser error keeps the whole document in .doc; it must not chain."""
        self.fake_aws(secret_string='{"password": "hunter2"')
        with self.assertRaises(RuntimeError) as ctx:
            aws_secrets.get_secret(SECRET_NAME, REGION)
        self.assertNotIn("hunter2", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertIsNone(ctx.exception.__context__)

    def test_failed_fetch_is_not_cached(self):
        self.fake_aws(error=FakeClientError("ThrottlingException"))
        with self.assertRaises(RuntimeError):
            aws_secrets.get_secret(SECRET_NAME, REGION)
        self.fake_aws(secret={"password": "s3cret"})
        self.assertEqual(aws_secrets.get_secret(SECRET_NAME, REGION)["password"], "s3cret")
        self.assertEqual(len(self.fetches), 2)

    def test_callers_get_a_copy_so_the_cache_cannot_be_modified(self):
        self.fake_aws(secret={"password": "s3cret"})
        aws_secrets.get_secret(SECRET_NAME, REGION)["password"] = "tampered"
        self.assertEqual(aws_secrets.get_secret(SECRET_NAME, REGION)["password"], "s3cret")


class TestReconnectAfterRotation(IsolatedTestCase):
    """DatabaseManager.connect() refetches the secret once when Postgres rejects its password."""

    BASE_ENV = TestSecretManagerOn.BASE_ENV

    def setUp(self):
        super().setUp()
        self.secret = {"username": "app", "password": "pw-old", "host": "rds.internal"}
        self.server_password = "pw-old"   # what the fake Postgres currently accepts
        self.connects = []                # password used by each connection attempt
        self._patch(mock.patch.object(database.psycopg, "connect", self._fake_connect))

    def _fake_connect(self, dsn, **kwargs):
        password = conninfo_to_dict(dsn).get("password")
        self.connects.append(password)
        if password != self.server_password:
            # Same wording psycopg 3.3 produced against a real server.
            raise psycopg.OperationalError(
                'connection failed: connection to server at "127.0.0.1", port 5432 failed: '
                'FATAL:  password authentication failed for user "app"'
            )
        return mock.Mock(name="connection")

    def rotate(self, new_password):
        self.secret["password"] = new_password
        self.server_password = new_password

    def test_rotation_mid_run_refetches_and_retries_once(self):
        self.fake_aws(secret=self.secret)
        database.DatabaseManager(database.get_dsn_from_env()).connect(init_schema=False)
        self.rotate("pw-new")
        manager = database.DatabaseManager(database.get_dsn_from_env())   # still the cached pw-old DSN
        manager.connect(init_schema=False)
        self.assertEqual(self.connects, ["pw-old", "pw-old", "pw-new"])
        self.assertEqual(len(self.fetches), 2)
        self.assertEqual(conninfo_to_dict(manager.dsn)["password"], "pw-new")

    def test_later_connections_use_the_refreshed_secret_without_another_fetch(self):
        self.fake_aws(secret=self.secret)
        database.DatabaseManager(database.get_dsn_from_env()).connect(init_schema=False)
        self.rotate("pw-new")
        database.DatabaseManager(database.get_dsn_from_env()).connect(init_schema=False)
        database.DatabaseManager(database.get_dsn_from_env()).connect(init_schema=False)
        self.assertEqual(self.connects, ["pw-old", "pw-old", "pw-new", "pw-new"])
        self.assertEqual(len(self.fetches), 2)

    def test_unchanged_secret_raises_without_retrying(self):
        """A password that is simply wrong is refetched once, then raised; no reconnect."""
        self.fake_aws(secret=self.secret)
        self.server_password = "changed-on-the-server-only"
        manager = database.DatabaseManager(database.get_dsn_from_env())
        with self.assertRaisesRegex(psycopg.OperationalError, "password authentication failed"):
            manager.connect(init_schema=False)
        self.assertEqual(self.connects, ["pw-old"])
        self.assertEqual(len(self.fetches), 2)

    def test_retries_at_most_once(self):
        self.fake_aws(secret=self.secret)
        manager = database.DatabaseManager(database.get_dsn_from_env())
        self.secret["password"] = "pw-new"          # the secret rotated...
        self.server_password = "pw-newer"           # ...and the server has already moved on again
        with self.assertRaisesRegex(psycopg.OperationalError, "password authentication failed"):
            manager.connect(init_schema=False)
        self.assertEqual(self.connects, ["pw-old", "pw-new"])

    def test_explicit_dsn_is_never_retried(self):
        self.fake_aws(secret=self.secret)
        manager = database.DatabaseManager("postgresql://app:typed-by-hand@rds.internal:5432/bulk-data")
        with self.assertRaises(psycopg.OperationalError):
            manager.connect(init_schema=False)
        self.assertEqual(self.connects, ["typed-by-hand"])
        self.assertEqual(self.fetches, [])

    def test_other_connection_errors_are_not_retried(self):
        self.fake_aws(secret=self.secret)
        manager = database.DatabaseManager(database.get_dsn_from_env())
        refused = psycopg.OperationalError("connection failed: Connection refused")
        with mock.patch.object(database.psycopg, "connect", side_effect=refused):
            with self.assertRaisesRegex(psycopg.OperationalError, "Connection refused"):
                manager.connect(init_schema=False)
        self.assertEqual(len(self.fetches), 1)


class TestSetupDbUsesTheSharedResolver(IsolatedTestCase):
    def test_setup_db_resolves_the_same_database_as_the_runner(self):
        """setup_db.py had its own DATABASE_URL-first resolver, so it could
        prepare a different database than the one the pipeline reads."""
        sys.modules.pop("setup_db", None)
        self.addCleanup(sys.modules.pop, "setup_db", None)
        with mock.patch("dotenv.load_dotenv", lambda *a, **k: False):
            setup_db = importlib.import_module("setup_db")
        self.assertIs(setup_db.get_dsn_from_env, database.get_dsn_from_env)
        self.assertFalse(hasattr(setup_db, "get_dsn"))


if __name__ == '__main__':
    unittest.main()
