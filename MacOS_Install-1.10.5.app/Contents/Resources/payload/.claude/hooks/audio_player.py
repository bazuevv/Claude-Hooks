"""Notification player command for the three supported operating systems."""
import os
import shutil
import sys


def player_command(path, seconds=0):
    if sys.platform == "darwin" and os.access("/usr/bin/afplay", os.X_OK):
        command = ["/usr/bin/afplay"]
        if seconds > 0:
            command += ["-t", str(seconds)]
        return command + [path]
    binary = shutil.which("ffplay")
    if not binary:
        return None
    command = [binary, "-nodisp", "-autoexit", "-loglevel", "quiet"]
    if seconds > 0:
        command += ["-t", str(seconds)]
    return command + ["-i", path]
