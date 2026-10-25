"""``python -m wiimote_mouse`` runs the daemon."""

import sys

from .daemon import main

sys.exit(main())
