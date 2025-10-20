import logging
from pathlib import Path

# add nullhandler to prevent a default configuration being used if the calling application doesn't set one
logger = logging.getLogger("aizk").addHandler(logging.NullHandler())
