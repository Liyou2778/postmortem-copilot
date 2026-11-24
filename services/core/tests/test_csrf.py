"""CSRF helpers and admin HTML token injection."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import csrf, render  # noqa: E402
from app.config import Settings  # noqa: E402


class TestCsrf(unittest.TestCase):
    def test_issue_and_verify(self):
        cfg = Settings(admin_password="secret")
        token = csrf.issue(cfg)
        self.assertIsNotNone(csrf.verify(token, cfg))
        self.assertIsNone(csrf.verify(token + "x", cfg))
        self.assertIsNone(csrf.verify(csrf.issue(Settings(admin_password="other")), cfg))

    def test_admin_page_injects_token_only_for_post_forms(self):
        render.set_csrf_token("token-123")
        page = render._page(
            "x", '<form method="post" action="/admin/x"><input name="a"></form>'
        )
        self.assertIn('name="csrf_token" value="token-123"', page)
        render.set_csrf_token("")


if __name__ == "__main__":
    unittest.main(verbosity=2)
