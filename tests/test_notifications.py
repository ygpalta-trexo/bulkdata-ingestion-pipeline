import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docdb_ingestion import notifications


class TestSendEmailGuards(unittest.TestCase):
    """send_email is best-effort: it reports failure, it never raises."""

    ENV_KEYS = (
        "EMAIL_CONFIG_HOST",
        "EMAIL_CONFIG_PORT",
        "EMAIL_CONFIG_AUTH_USER",
        "EMAIL_CONFIG_AUTH_PASSWORD",
        "EMAIL_RECIPIENT",
    )

    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _configure(self, port=None):
        os.environ["EMAIL_CONFIG_HOST"] = "smtp.invalid"
        os.environ["EMAIL_CONFIG_AUTH_USER"] = "noreply@example.com"
        os.environ["EMAIL_CONFIG_AUTH_PASSWORD"] = "secret"
        if port is not None:
            os.environ["EMAIL_CONFIG_PORT"] = port

    def test_no_recipient_returns_false(self):
        self._configure()
        self.assertFalse(notifications.send_email("", "subject", "<p>body</p>"))

    def test_missing_smtp_settings_return_false(self):
        self.assertFalse(notifications.send_email("ops@example.com", "subject", "<p>body</p>"))

    def test_malformed_port_falls_back_instead_of_raising(self):
        """An empty or typo'd EMAIL_CONFIG_PORT is a .env mistake, not a crash."""
        for bad_port in ("", "   ", "not-a-number"):
            with self.subTest(port=bad_port):
                self._configure(port=bad_port)
                # smtp.invalid never resolves, so this returns False via the
                # connection error — the point is that int() did not raise first.
                self.assertFalse(
                    notifications.send_email("ops@example.com", "subject", "<p>body</p>")
                )

    def test_unreachable_server_returns_false(self):
        self._configure(port="587")
        self.assertFalse(notifications.send_email("ops@example.com", "subject", "<p>body</p>"))

    def test_default_recipient_reads_env(self):
        self.assertEqual(notifications.get_default_recipient(), "")
        os.environ["EMAIL_RECIPIENT"] = " ops@example.com, team@example.com "
        self.assertEqual(
            notifications.get_default_recipient(), "ops@example.com, team@example.com"
        )


if __name__ == '__main__':
    unittest.main()
