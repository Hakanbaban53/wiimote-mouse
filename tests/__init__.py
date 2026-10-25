import logging

# Keep the daemon's log output out of the test run (assertLogs still sees it).
logging.getLogger("wiimote-mouse").addHandler(logging.NullHandler())
