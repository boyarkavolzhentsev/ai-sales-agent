"""``python -m app.runtime <command>``: see ``app.runtime.cli``."""

import os
import sys

from app.runtime.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ, sys.stdout))
