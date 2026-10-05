#!/usr/bin/env python3
"""hintbar's entry point, run by hintbar.tmux, prefix r and the live hook.
Kept to syntax old Pythons parse, so they get a message, not a traceback."""
import subprocess
import sys

if sys.version_info < (3, 8):
    subprocess.call(['tmux', 'display-message', '-d', '0',
                     'hintbar: needs python3 3.8 or newer; this is ' + sys.version.split()[0]])
    sys.exit(0)

from hintbar.main import main  # noqa: E402

main()
