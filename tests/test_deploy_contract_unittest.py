from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
START_SCRIPT = (ROOT / "deploy" / "start_brx_sync.sh").read_text(encoding="utf-8")
COMPOSE_FILE = (ROOT / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")


class DeployContractTest(unittest.TestCase):
    def test_deploy_uses_selected_image_tag_and_defaults_writes_off(self) -> None:
        self.assertIn('export IMAGE_TAG="${IMAGE_TAG:-latest}"', START_SCRIPT)
        self.assertIn(
            'export CARDTRADER_WRITES_ENABLED="${CARDTRADER_WRITES_ENABLED:-false}"',
            START_SCRIPT,
        )
        self.assertEqual(
            COMPOSE_FILE.count("${ECR_REGISTRY}/ebartex-sync:${IMAGE_TAG:-latest}"),
            2,
        )
        self.assertEqual(
            COMPOSE_FILE.count(
                "CARDTRADER_WRITES_ENABLED=${CARDTRADER_WRITES_ENABLED:-false}"
            ),
            2,
        )

    def test_execution_policy_migration_runs_after_trade_foundations(self) -> None:
        foundations = START_SCRIPT.index("20260714_trade_inventory_foundations.sql")
        visibility = START_SCRIPT.index("20260714_trade_inventory_visibility.sql")
        execution_policy = START_SCRIPT.index("20260716_cardtrader_execution_policy.sql")

        self.assertLess(foundations, visibility)
        self.assertLess(visibility, execution_policy)
        self.assertIn('psql "$DB_URL" -v ON_ERROR_STOP=1', START_SCRIPT)
        self.assertIn("up -d --force-recreate --remove-orphans", START_SCRIPT)


if __name__ == "__main__":
    unittest.main()
