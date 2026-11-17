"""Shared test helpers: temp workspaces and Unicode fixture construction."""

import os
import shutil
import tempfile

# All non-ASCII data is constructed with chr() so test sources stay ASCII.
FULLWIDTH_A = chr(0xFF21)
FULLWIDTH_1 = chr(0xFF11)
E_ACUTE_PRE = chr(0x00E9)
E_ACUTE_DEC = "e" + chr(0x0301)
SHARP_S = chr(0x00DF)
CAPITAL_I_DOT = chr(0x0130)
SOFT_HYPHEN = chr(0x00AD)
CYRILLIC_A = chr(0x0430)
IDEOGRAPHIC_SPACE = chr(0x3000)


class TempWorkspace:
    """Create and clean up an isolated test directory under the system temp."""

    def __init__(self, prefix="login-migration-test-"):
        self.root = tempfile.mkdtemp(prefix=prefix)

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)
