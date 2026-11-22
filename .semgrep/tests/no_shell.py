# Fixture for sdlc-no-shell.
import os
import subprocess


def bad(cmd):
    # ruleid: sdlc-no-shell
    subprocess.run(cmd, shell=True, check=False)
    # ruleid: sdlc-no-shell
    subprocess.Popen(cmd, stdout=None, shell=True)
    # ruleid: sdlc-no-shell
    os.system(cmd)
    # ruleid: sdlc-no-shell
    return os.popen(cmd)


def good(argv):
    # ok: sdlc-no-shell
    subprocess.run(argv, check=False)
    # ok: sdlc-no-shell
    subprocess.run(argv, shell=False, check=False)
