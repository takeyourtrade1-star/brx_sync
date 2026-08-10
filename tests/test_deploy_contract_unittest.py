import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START_SCRIPT = (ROOT / "deploy" / "start_brx_sync.sh").read_text(encoding="utf-8")
COMPOSE_FILE = (ROOT / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


class DeployContractTest(unittest.TestCase):
    def test_external_egress_and_redis_launch_gates_are_documented(self) -> None:
        gates = (ROOT / "docs" / "PRODUCTION_SECURITY_GATES.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("BLOCKING", gates)
        self.assertIn("TCP 5432", gates)
        self.assertIn("managed private Redis", gates)
        self.assertIn("TLS", gates)
        self.assertIn("ACL", gates)
        self.assertIn("bridge still has NAT egress", COMPOSE_FILE)
        self.assertNotIn('log_ok "REDIS_URL=${REDIS_URL}"', START_SCRIPT)

    def test_ci_actions_are_commit_pinned_and_runtime_is_python_312(self) -> None:
        workflows = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
        self.assertTrue(workflows)
        for workflow in workflows:
            content = workflow.read_text(encoding="utf-8")
            for reference in re.findall(r"(?m)^\s*-?\s*uses:\s*([^\s#]+)", content):
                self.assertRegex(reference, r"^[^@]+@[0-9a-f]{40}$", workflow.name)
            self.assertNotIn("ubuntu-latest", content)
        gate = (ROOT / ".github" / "workflows" / "security-test.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('python-version: "3.12"', gate)
        self.assertIn("vars.PYTHON_BASE_IMAGE", gate)
        self.assertIn("vars.POSTGRES_TEST_IMAGE", gate)
        self.assertIn("vars.REDIS_TEST_IMAGE", gate)
        self.assertIn("Run blocking security regressions", gate)
        self.assertIn("pip-audit==2.10.1", gate)
        self.assertIn("python -m pip_audit --requirement requirements.txt", gate)
        self.assertIn("python -m pip_audit --requirement requirements-dev.txt", gate)
        requirements_dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
        self.assertIn("-r requirements.txt", requirements_dev)

    def test_deploy_requires_immutable_image_and_defaults_writes_off(self) -> None:
        self.assertIn('IMAGE_DIGEST="${IMAGE_DIGEST:?', START_SCRIPT)
        self.assertIn('export BRX_SYNC_IMAGE="${ECR_REGISTRY}/ebartex-sync@${IMAGE_DIGEST}"', START_SCRIPT)
        self.assertIn(
            'export CARDTRADER_WRITES_ENABLED="${CARDTRADER_WRITES_ENABLED:-false}"',
            START_SCRIPT,
        )
        self.assertEqual(
            COMPOSE_FILE.count("${BRX_SYNC_IMAGE:?set immutable BRX_SYNC_IMAGE with @sha256 digest}"),
            4,
        )
        self.assertNotIn(":latest", COMPOSE_FILE)
        self.assertNotIn("image: redis:", COMPOSE_FILE)
        self.assertIn(
            "${REDIS_IMAGE:?set immutable REDIS_IMAGE with @sha256 digest}",
            COMPOSE_FILE,
        )
        self.assertIn('REDIS_IMAGE="${REDIS_IMAGE:?', START_SCRIPT)
        self.assertIn("@sha256:[0-9a-f]{64}", START_SCRIPT)
        self.assertEqual(
            COMPOSE_FILE.count("CARDTRADER_WRITES_ENABLED=${CARDTRADER_WRITES_ENABLED:-false}"),
            2,
        )

    def test_production_jwt_verification_defaults_to_strict(self) -> None:
        self.assertIn('JWT_REQUIRE_ISSUER_AUDIENCE:-true}', START_SCRIPT)
        self.assertIn('JWT_REQUIRE_JTI:-true}', START_SCRIPT)
        self.assertEqual(COMPOSE_FILE.count("JWT_LEGACY_ROLLOUT_ACK"), 2)
        self.assertEqual(COMPOSE_FILE.count("JWT_LEGACY_ROLLOUT_EXPIRES_AT"), 2)

    def test_execution_policy_migration_runs_after_trade_foundations(self) -> None:
        foundations = COMPOSE_FILE.index("20260714_trade_inventory_foundations.sql")
        visibility = COMPOSE_FILE.index("20260714_trade_inventory_visibility.sql")
        execution_policy = COMPOSE_FILE.index("20260716_cardtrader_execution_policy.sql")
        outbound_create = COMPOSE_FILE.index("20260810_cardtrader_outbound_create.sql")
        schema_job = START_SCRIPT.index("brx-sync-schema-migrate")
        credential_rotation = START_SCRIPT.index("brx-sync-credential-rotation")

        self.assertLess(foundations, visibility)
        self.assertLess(visibility, execution_policy)
        self.assertLess(execution_policy, outbound_create)
        self.assertLess(schema_job, credential_rotation)
        self.assertIn("run --rm --no-deps brx-sync-schema-migrate", START_SCRIPT)
        self.assertIn("run --rm --no-deps brx-sync-credential-rotation", START_SCRIPT)
        self.assertNotIn("run --rm --no-deps brx-sync-api", START_SCRIPT)
        self.assertIn("up -d --force-recreate --remove-orphans", START_SCRIPT)
        self.assertIn("stop brx-sync-api brx-sync-worker", START_SCRIPT)
        self.assertIn('if [[ "$ready" != "true" ]]', START_SCRIPT)
        self.assertIn('exit 1', START_SCRIPT)
        self.assertNotIn("169.254.169.254", START_SCRIPT)

        previous_key = START_SCRIPT.index('get_ssm "/prod/ebartex/fernet_key"')
        clear_previous_key = START_SCRIPT.index("unset FERNET_PREVIOUS_KEYS", credential_rotation)
        launch = START_SCRIPT.index("up -d --force-recreate --remove-orphans")
        self.assertLess(previous_key, credential_rotation)
        self.assertLess(credential_rotation, clear_previous_key)
        self.assertLess(clear_previous_key, launch)

    def test_deploy_uses_dedicated_database_roles_and_passwords(self) -> None:
        self.assertIn("/prod/ebartex/brx_sync_db_user", START_SCRIPT)
        self.assertIn("/prod/ebartex/brx_sync_db_password", START_SCRIPT)
        self.assertIn("/prod/ebartex/brx_sync_mysql_user", START_SCRIPT)
        self.assertIn("/prod/ebartex/brx_sync_mysql_password", START_SCRIPT)
        self.assertIn("/prod/ebartex/brx_sync_migration_db_user", START_SCRIPT)
        self.assertIn("/prod/ebartex/brx_sync_migration_db_password", START_SCRIPT)
        self.assertIn('SYNC_MIGRATION_DB_USER" != "$DB_USER', START_SCRIPT)
        self.assertNotIn('get_ssm "/prod/ebartex/db_password"', START_SCRIPT)
        self.assertNotIn('get_ssm "/prod/ebartex/mysql_password"', START_SCRIPT)

    def test_databases_use_the_verified_amazon_rds_ca_bundle(self) -> None:
        ca_path = "/etc/ssl/certs/aws-rds-global-bundle.pem"

        self.assertIn(
            "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem",
            START_SCRIPT,
        )
        self.assertIn("--proto '=https' --tlsv1.2", START_SCRIPT)
        self.assertIn("Amazon RDS eu-south-1 Root CA RSA2048 G1", START_SCRIPT)
        self.assertEqual(COMPOSE_FILE.count(f"MYSQL_SSL_CA_FILE={ca_path}"), 2)
        self.assertEqual(COMPOSE_FILE.count(f"DATABASE_SSL_CA_FILE={ca_path}"), 3)
        self.assertIn(f"PGSSLROOTCERT={ca_path}", COMPOSE_FILE)
        self.assertEqual(COMPOSE_FILE.count(f"{ca_path}:ro"), 4)

    def test_production_requires_service_scoped_caller_map(self) -> None:
        self.assertIn(
            'get_ssm "/prod/ebartex/sync_internal_caller_tokens"',
            START_SCRIPT,
        )
        self.assertNotIn('get_ssm "/prod/ebartex/internal_api_token"', START_SCRIPT)
        self.assertIn('export INTERNAL_API_TOKEN=""', START_SCRIPT)
        self.assertIn("INTERNAL_API_TOKEN=", COMPOSE_FILE)
        self.assertIn(
            "INTERNAL_CALLER_TOKENS=${INTERNAL_CALLER_TOKENS:?required scoped caller map}",
            COMPOSE_FILE,
        )

    def test_cloud_identity_and_database_topology_are_runtime_validated(self) -> None:
        self.assertIn("aws sts get-caller-identity", START_SCRIPT)
        self.assertIn("EXPECTED_AWS_ACCOUNT_ID", START_SCRIPT)
        self.assertIn(
            '[[ "$CALLER_ACCOUNT_ID" == "$EXPECTED_AWS_ACCOUNT_ID" ]]',
            START_SCRIPT,
        )
        self.assertIn(
            'ECR_REGISTRY="${CALLER_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"',
            START_SCRIPT,
        )
        self.assertIn('get_ssm "/prod/ebartex/auth_db_host"', START_SCRIPT)
        self.assertIn('get_ssm "/prod/ebartex/auth_db_name"', START_SCRIPT)
        self.assertIn('get_ssm "/prod/ebartex/search_mysql_host"', START_SCRIPT)
        self.assertIn('get_ssm "/prod/ebartex/search_mysql_database"', START_SCRIPT)
        self.assertIn("validate_database_host", START_SCRIPT)
        self.assertIn("rds.amazonaws.com", START_SCRIPT)
        self.assertIn("address.is_private", START_SCRIPT)
        self.assertIsNone(re.search(r"(?<![0-9])[0-9]{12}(?![0-9])", START_SCRIPT))
        script_without_truststore = START_SCRIPT.replace(
            "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem",
            "",
        )
        self.assertNotRegex(
            script_without_truststore,
            r"[A-Za-z0-9.-]+\.rds\.amazonaws\.com",
        )
        self.assertNotIn('DB_NAME="ebartex_', START_SCRIPT)

    def test_container_build_requires_a_caller_supplied_base_image(self) -> None:
        self.assertIn("ARG PYTHON_BASE_IMAGE", DOCKERFILE)
        self.assertIn("FROM ${PYTHON_BASE_IMAGE}", DOCKERFILE)
        self.assertIn("PYTHON_BASE_IMAGE must use @sha256", DOCKERFILE)
        self.assertNotRegex(DOCKERFILE, r"(?m)^FROM\s+python:[^\s]+")
        self.assertIn("ca-certificates postgresql-client", DOCKERFILE)

    def test_production_services_are_not_public_and_run_hardened(self) -> None:
        self.assertIn(
            '"${SERVICE_BIND_IP:?required private service bind IP}:8002:8000"',
            COMPOSE_FILE,
        )
        self.assertNotIn('- "8002:8000"', COMPOSE_FILE)
        self.assertIn("validate_service_bind_ip", START_SCRIPT)
        self.assertIn("SERVICE_BIND_IP is not assigned to this host", START_SCRIPT)
        self.assertIn('"http://${SERVICE_BIND_IP}:8002/health/ready"', START_SCRIPT)
        self.assertIn('-H "Host: sync.ebartex.com"', START_SCRIPT)
        self.assertEqual(COMPOSE_FILE.count("- ALLOWED_ORIGINS"), 2)
        worker = COMPOSE_FILE.split("  brx-sync-worker:", 1)[1].split(
            "\n  # One-shot", 1
        )[0]
        self.assertIn("healthcheck:\n      disable: true", worker)
        self.assertEqual(COMPOSE_FILE.count("read_only: true"), 5)
        self.assertEqual(COMPOSE_FILE.count("no-new-privileges:true"), 5)
        self.assertEqual(COMPOSE_FILE.count("cap_drop:"), 5)
        self.assertIn("--no-server-header", COMPOSE_FILE)
        for flag in (
            "--no-proxy-headers",
            "--limit-concurrency 128",
            "--limit-max-requests 10000",
            "--timeout-keep-alive 5",
            "--timeout-graceful-shutdown 30",
        ):
            self.assertIn(flag, COMPOSE_FILE)
        for flag in (
            '"--no-proxy-headers"',
            '"--limit-concurrency"',
            '"--limit-max-requests"',
            '"--timeout-keep-alive"',
            '"--timeout-graceful-shutdown"',
        ):
            self.assertIn(flag, DOCKERFILE)
        self.assertIn("REQUEST_MAX_BODY_MESSAGES=1024", COMPOSE_FILE)
        self.assertIn("--maxmemory-policy", COMPOSE_FILE)
        self.assertNotIn("ports:\n      - \"6379:6379\"", COMPOSE_FILE)
        redis_service = COMPOSE_FILE.rsplit("  brx-sync-redis:", 1)[1].split(
            "networks:", 1
        )[0]
        self.assertIn('user: "999:1000"', redis_service)

    def test_production_root_does_not_fingerprint_version(self) -> None:
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn(
            'if settings.ENVIRONMENT in {"development", "test"}:',
            main,
        )
        self.assertNotIn(
            '"version": settings.APP_VERSION,\n        "status": "running"',
            main,
        )

    def test_maintenance_jobs_have_exact_secret_boundaries(self) -> None:
        schema = COMPOSE_FILE.split("  brx-sync-schema-migrate:", 1)[1].split(
            "  brx-sync-credential-rotation:", 1
        )[0]
        rotation = COMPOSE_FILE.split("  brx-sync-credential-rotation:", 1)[1].split(
            "  brx-sync-redis:", 1
        )[0]

        for required in ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD"):
            self.assertIn(required, schema)
        for forbidden in (
            "DATABASE_URL",
            "FERNET_KEY",
            "MYSQL_",
            "REDIS_URL",
            "JWT_",
            "INTERNAL_",
        ):
            self.assertNotIn(forbidden, schema)

        for required in ("DATABASE_URL", "DATABASE_SSL_CA_FILE", "FERNET_KEY"):
            self.assertIn(required, rotation)
        for forbidden in ("MYSQL_", "REDIS_URL", "JWT_", "INTERNAL_", "PGPASSWORD"):
            self.assertNotIn(forbidden, rotation)
        for job in (schema, rotation):
            self.assertIn('profiles: ["maintenance"]', job)
            self.assertIn("read_only: true", job)
            self.assertIn("no-new-privileges:true", job)
            self.assertIn("disable: true", job)
            self.assertIn("brx-sync-maintenance-net", job)
            self.assertNotIn("- brx-sync-net", job)

        rotation_module = (
            ROOT / "app" / "maintenance" / "rotate_credentials.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("app.core.database", rotation_module)
        self.assertNotIn("app.core.config", rotation_module)
        self.assertNotIn("get_encryption_manager", rotation_module)

    def test_each_database_pool_has_an_explicit_small_production_cap(self) -> None:
        self.assertIn("DB_POOL_SIZE=3", COMPOSE_FILE)
        self.assertIn("DB_MAX_OVERFLOW=2", COMPOSE_FILE)
        self.assertEqual(COMPOSE_FILE.count("DB_SYNC_POOL_SIZE=1"), 2)
        self.assertIn("DB_SYNC_MAX_OVERFLOW=0", COMPOSE_FILE)
        self.assertEqual(COMPOSE_FILE.count("DB_ISOLATED_POOL_SIZE=1"), 2)
        self.assertIn("DB_ISOLATED_MAX_OVERFLOW=0", COMPOSE_FILE)
        self.assertIn("MYSQL_POOL_SIZE=3", COMPOSE_FILE)
        self.assertIn("MYSQL_POOL_SIZE=2", COMPOSE_FILE)
        database = (ROOT / "app" / "core" / "database.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("pool_size=settings.DB_SYNC_POOL_SIZE", database)
        self.assertIn("max_overflow=settings.DB_SYNC_MAX_OVERFLOW", database)
        self.assertIn("pool_size=settings.DB_ISOLATED_POOL_SIZE", database)
        self.assertNotIn("pool_size=5,", database)
        self.assertNotIn("max_overflow=10,", database)

    def test_production_image_excludes_test_ui_and_local_secrets(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for pattern in ("static/", "tests/", ".env.*", "secrets/", "*.pem", "*.key"):
            self.assertIn(pattern, dockerignore)

    def test_developer_helpers_do_not_evaluate_env_or_remote_scripts(self) -> None:
        setup = (ROOT / "setup_macos.sh").read_text(encoding="utf-8")
        test_setup = (ROOT / "test_setup.sh").read_text(encoding="utf-8")
        start_local = (ROOT / "start_local.sh").read_text(encoding="utf-8")
        self.assertNotIn("curl -fsSL", setup)
        self.assertNotIn("MYSQL_USER=root", setup)
        self.assertNotIn("MYSQL_PASSWORD=root", setup)
        self.assertNotIn('eval "$command"', test_setup)
        self.assertNotIn("black --check app/core/exceptions.py app/core/logging.py 2>/dev/null || true", test_setup)
        self.assertNotIn("export $(cat .env", start_local)
        test_ui = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("Math.random", test_ui)
        self.assertIn("globalThis.crypto.randomUUID()", test_ui)

    def test_worker_results_do_not_persist_raw_exception_messages(self) -> None:
        outbox = (ROOT / "app" / "tasks" / "outbox_tasks.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("error=str(exc)", outbox)
        self.assertNotIn('"error": str(exc)', outbox)
        self.assertNotIn('f"{type(exc).__name__}: {exc}"', outbox)

    def test_untrusted_jwt_claims_are_not_written_to_logs(self) -> None:
        dependencies = (ROOT / "app" / "api" / "dependencies.py").read_text(
            encoding="utf-8"
        )
        handlers = (ROOT / "app" / "core" / "exception_handlers.py").read_text(
            encoding="utf-8"
        )
        mapper = (ROOT / "app" / "services" / "blueprint_mapper.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("Invalid token type: {token_type}", dependencies)
        self.assertNotIn("token={user_id_from_token}", dependencies)
        self.assertIn("token type is not access", dependencies)
        self.assertNotIn("exc.detail", handlers)
        self.assertNotIn('"path": request.url.path', handlers)
        self.assertNotIn(": {cached}", mapper)

    def test_public_readiness_probe_is_timeout_bounded_and_single_flight(self) -> None:
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        cache = (ROOT / "app" / "core" / "probe_cache.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("readiness_probe = AsyncProbeCache", main)
        self.assertIn("asyncio.wait_for", cache)
        self.assertIn("self._in_flight", cache)


if __name__ == "__main__":
    unittest.main()
