"""Flow Steward runtime images create files group-writable (ADR 0029, owner decision B).

Compact and Scalable read and write the same artifact and extension trees
through fixed supplementary groups (10100, 10103), on directories that carry the
setgid bit so a new entry keeps the tree's group. Under the default umask 022 a new file
lost its group-write bit, and the other profile -- or another role of the same one --
could read it and not change it.

Installed into the runtime images' site-packages, so every Python process gets it
whatever its entrypoint: `compose run --entrypoint python ...` one-shots, the Compact
supervisor and every role it starts, and the subprocesses they start (pip, extension
processes), which inherit the umask. Files that must stay private are written with an explicit mode
(0600/0700), which a umask cannot widen.
"""

import os

os.umask(0o002)
