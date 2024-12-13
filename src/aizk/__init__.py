import logging
from pathlib import Path

# assumes:
# ai-zettelkasten
# ├ src/
# | └ ai-zk
# |   └ __init__.py - (this file)
# └ VERSION
with open(Path(__file__).parent.parent / "VERSION", "r") as f:
    __version__ = f.readline().strip()

# add nullhandler to prevent a default configuration being used if the calling application doesn't set one
logging.getLogger(__name__).addHandler(logging.NullHandler())
