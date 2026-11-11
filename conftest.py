"""Let pytest run from a checkout whose directory name is not a Python identifier.

The repository root is the Hermes plugin package (``__init__.py`` uses a relative
import). pytest treats the root as a package and imports that file when it sets
the collection up. For a directory name such as the default ``hermes-engagements``
it cannot build a package name, imports the file as a bare ``__init__`` and fails
on the relative import before any test runs. Pre-seeding ``__init__`` makes that
import a no-op. A valid directory name is untouched, and nothing here is imported
by the plugin or the tests.
"""

import sys
import types
from pathlib import Path

if not Path(__file__).resolve().parent.name.isidentifier():
    sys.modules.setdefault("__init__", types.ModuleType("__init__"))
