# Fixture for sdlc-check-except-returns-clean.
CLEAN = 0


def cannot(msg):
    raise RuntimeError(msg)


def t_bad(path):
    try:
        return open(path).read() and 1
    except OSError:
        # ruleid: sdlc-check-except-returns-clean
        return CLEAN


def t_bad_literal(path):
    try:
        open(path)
    except OSError:
        # ruleid: sdlc-check-except-returns-clean
        return 0
    return 1


def t_good(path):
    try:
        open(path)
    except OSError as exc:
        # ok: sdlc-check-except-returns-clean
        cannot(f"cannot read {path}: {exc}")
    return CLEAN


def helper(path):
    try:
        open(path)
    except OSError:
        # ok: sdlc-check-except-returns-clean
        return 0
    return 1


def t_bad_as(path):
    try:
        open(path)
    except OSError as exc:
        print(exc)
        # ruleid: sdlc-check-except-returns-clean
        return CLEAN
    return 1


def t_bad_tuple(path):
    try:
        open(path)
    except (OSError, ValueError):
        # ruleid: sdlc-check-except-returns-clean
        return CLEAN
    return 1


def t_bad_bare(path):
    try:
        open(path)
    except:  # noqa: E722
        # ruleid: sdlc-check-except-returns-clean
        return 0
    return 1


def t_good_trailing(path):
    try:
        open(path)
    except OSError:
        cannot(f"cannot read {path}")
    # ok: sdlc-check-except-returns-clean
    return CLEAN
