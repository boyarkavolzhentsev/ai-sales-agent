"""``python -m app.runtime <command>``: see ``app.runtime.cli``."""

import logging
import os
import sys

from app.runtime.cli import main

if __name__ == "__main__":
    # Plain logs to stderr (JSON results stay alone on stdout). Every app logger writes
    # identifiers, codes, counts and timings only: no message bodies, prompts, vectors or keys.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sys.exit(main(sys.argv[1:], os.environ, sys.stdout))
