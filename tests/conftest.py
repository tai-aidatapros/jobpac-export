"""
Register stub modules for heavy native dependencies so tests run without them installed.
The real modules are fully replaced by @patch in each test anyway.
"""

import sys
from unittest.mock import MagicMock

for _mod in (
    "jaydebeapi", "jpype", "jpype._jclass", "pyodbc",
    "boto3", "botocore", "botocore.exceptions",
):
    sys.modules.setdefault(_mod, MagicMock())
