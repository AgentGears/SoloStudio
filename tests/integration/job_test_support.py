from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from solostudio.app.bootstrap import bootstrap
from solostudio.kernel.clock import FixedClock
from solostudio.kernel.ids import SequenceIdSource


class JobTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.ids = SequenceIdSource()
        self.kernel = bootstrap(self.root, clock=FixedClock(), ids=self.ids)
        self.project_id = self.kernel.productions.create_project("M0", "project")
        self.production_id = self.kernel.productions.create_production(self.project_id, "Explainer", "production")

    def tearDown(self) -> None:
        try:
            self.kernel.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def capture_revision(self):
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=0,
            idempotency_key="script",
            action="set_script",
            command_input={"text": "seed"},
        )
        return self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=1,
            idempotency_key="capture",
        )
