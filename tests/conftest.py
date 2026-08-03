"""Pin the configuration the suite runs under, before anything reads it.

Found the hard way: `.env` is a developer's local file, and the app reads it.
Point it at the second scenario pack to look at something, and seven unrelated
API tests start failing with assertions about airports. The failures are
confusing, they have nothing to do with whatever you were changing, and the fix
is a file you forgot you edited.

So the suite does not inherit ambient configuration. Environment variables take
precedence over the `.env` file in pydantic-settings, and this module is
imported before any test module, so setting them here wins over whatever is on
disk.

Deliberately pinned:

  SCENARIO           the API tests assert on the airport pack's contents.
                     Cross-pack behaviour is covered by parametrised tests in
                     test_scenario_equivalence.py, which read the packs
                     directly rather than through the server.

  ANTHROPIC_API_KEY  empty, so the suite always exercises the keyless posture
                     and never makes a network call. A developer with a real
                     key configured gets the same result as CI, and a test can
                     never quietly start billing someone.
"""

from __future__ import annotations

import os

os.environ["SCENARIO"] = "airport"
os.environ["ANTHROPIC_API_KEY"] = ""
