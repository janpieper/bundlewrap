# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from subprocess import CalledProcessError, check_output, STDOUT


def get_git_rev():
    try:
        return check_output(
            "git rev-parse HEAD",
            shell=True,
            stderr=STDOUT,
        ).strip()
    except CalledProcessError:
        return None


def get_rev():
    for scm_rev in (get_git_rev,):
        rev = scm_rev()
        if rev is not None:
            return rev
    return None
