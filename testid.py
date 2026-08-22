"""Stable, URL- and HTML-safe test identifiers.

The registry keys tests by module dot-path, e.g.

    OWC_CLONETEST2.__testlist__OWC_CLONETEST2_86box

which is precise but drags "__testlist__" and underscores through URLs, HTML
attributes and query strings. So the v1 API exposes a slug instead:

    OWC_CLONETEST2.__testlist__OWC_CLONETEST2_86box  ->  owc-clonetest2.86box
    dosboxtest.__testlist__dosbox_buildtest          ->  dosboxtest.dosbox-buildtest

Rules: lowercase, "_" -> "-", the "__testlist__" marker dropped, and the
project segment stripped off the front of the file segment when it merely
repeats it. The project/file boundary stays a dot, so a slug keeps the same
shape as the dot-path it came from -- and, being URL-unreserved, survives a
URL intact instead of turning into %2F. Slugs derive purely from the module
path, so they are stable across restarts and across registry reloads. If two
modules ever collapse onto the same slug, both fall back to the unstripped
form so ids stay unique.

The slug is also filename-safe, so reports on disk are named by it too.

Every lookup path accepts either form, so existing callers that pass a module
dot-path keep working.
"""

import re

TESTLIST_MARKER = "__testlist__"
SEPARATOR = "."


def _kebab(text):
    """Lowercase, collapse separators to single hyphens, drop anything else."""
    text = (text or "").strip().lower()
    text = re.sub(r"[\s_./]+", "-", text)
    text = re.sub(r"[^a-z0-9\-]", "", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")


def _split(modname):
    """Module dot-path -> (project segment, file segment without the marker)."""
    parts = modname.split(".")
    proj = parts[0] if len(parts) > 1 else ""
    leaf = parts[-1]
    if leaf.startswith(TESTLIST_MARKER):
        leaf = leaf[len(TESTLIST_MARKER):]
    return proj, leaf


def _candidates(modname):
    """Preferred slug first, then the unambiguous fallback."""
    proj, leaf = _split(modname)
    if not proj:
        return [_kebab(leaf)]

    short = leaf
    prefix = proj.lower() + "_"
    if leaf.lower().startswith(prefix) and len(leaf) > len(prefix):
        short = leaf[len(prefix):]

    full = f"{_kebab(proj)}{SEPARATOR}{_kebab(leaf)}"
    if short == leaf:
        return [full]
    return [f"{_kebab(proj)}{SEPARATOR}{_kebab(short)}", full]


def build_index(modnames):
    """Map every module name to a unique slug.

    Two passes so the outcome never depends on iteration order: claim the
    preferred slug only where exactly one module wants it, then give everyone
    else their unambiguous fallback.
    """
    modnames = sorted(modnames)
    wanted = {m: _candidates(m) for m in modnames}

    counts = {}
    for m in modnames:
        counts[wanted[m][0]] = counts.get(wanted[m][0], 0) + 1

    slug_to_mod, mod_to_slug = {}, {}
    for m in modnames:
        options = wanted[m]
        chosen = options[0] if counts[options[0]] == 1 else options[-1]
        # Last-resort disambiguation; only reachable if a fallback also clashes.
        base, n = chosen, 2
        while chosen in slug_to_mod:
            chosen = f"{base}-{n}"
            n += 1
        slug_to_mod[chosen] = m
        mod_to_slug[m] = chosen
    return slug_to_mod, mod_to_slug


def slug_for(modname, modnames):
    """Module dot-path -> its slug. Falls back to the dot-path if unregistered."""
    _, mod_to_slug = build_index(modnames)
    return mod_to_slug.get(modname, modname)


def resolve(test_id, modnames):
    """Accept a slug or a module dot-path; return the module name or None."""
    modnames = list(modnames)
    if test_id in modnames:
        return test_id
    slug_to_mod, _ = build_index(modnames)
    return slug_to_mod.get(test_id)


def group_key(display_name, system):
    """Row identity: tests sharing a testname and system render as one row."""
    return f"{_kebab(display_name)}--{_kebab(system)}"
