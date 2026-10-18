import sys, os
import re
import importlib.util
import os
import inspect
import threading
from markupsafe import escape, Markup
_step_counter = 0

testfile_registry = {}
registry_map = {}

# reload_tests() rebuilds testfile_registry by clearing it and then
# re-populating it one file at a time as it walks the filesystem and imports
# each testlist (register_testfile() writes an entry per import). That's a
# long window with the dict in a half-built state. reload_tests() can be
# triggered concurrently from more than one thread -- a request handler
# (apiv1.list_tests et al.) and a background run worker (runhelper.py) both
# call it -- and anything iterating testfile_registry.items()/.values() while
# another thread is mid-rebuild hits "dictionary changed size during
# iteration". Every writer and every read that iterates the registry must
# hold this lock.
registry_lock = threading.RLock()


helperdir = "/testsrc/pyhelpers"
TESTSRC_BASE = "/testsrc"

TESTPARENT_FILE = "__testparent__.py"
REQUIRED_PARENT_KEYS = ("name", "archtype", "platform")

# Optional PARENT keys. Unlike the required three, these only steer how the
# container page presents itself, so a stub that omits them behaves exactly
# as it did before they existed.
OPTIONAL_PARENT_KEYS = ("description", "batch_run_all", "batch_run_failed", "batch_run_tools", "view",
                        "build_tracker", "ab_testing")

# Layouts the container page can open in: one card per child variant (the
# original), or one row per child variant in a report-style table. A
# container with a dozen children is unreadable as cards.
CONTAINER_VIEWS = ("cards", "table")
DEFAULT_CONTAINER_VIEW = "cards"

_parent_cache = {}


class ParentError(Exception):
    """A testlist's parent declaration is missing, malformed, or mismatched."""


def load_parent(dirpath):
    """Read a directory's __testparent__.py and return its PARENT dict.

    Returns None when the directory has no stub at all 
    """
    path = os.path.join(dirpath, TESTPARENT_FILE)
    if path in _parent_cache:
        return _parent_cache[path]

    parent = None
    if os.path.exists(path):
        modname = "_testparent_%s" % abs(hash(path))
        try:
            spec = importlib.util.spec_from_file_location(modname, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            parent = getattr(mod, "PARENT", None)
        except Exception as e:
            raise ParentError(f"{path} failed to load: {e}")
        finally:
            sys.modules.pop(modname, None)

        if not isinstance(parent, dict):
            raise ParentError(f"{path} defines no PARENT dict")
        missing = [k for k in REQUIRED_PARENT_KEYS if not parent.get(k)]
        if missing:
            raise ParentError(f"{path} PARENT is missing {', '.join(missing)}")

    _parent_cache[path] = parent
    return parent


def parent_view(parent):
    """Which layout a container's page opens in, from its PARENT stub.

    Anything unset or unrecognised falls back to the card view, so a typo in
    a stub degrades to the old behaviour instead of a blank page.
    """
    view = str((parent or {}).get("view") or "").strip().lower()
    return view if view in CONTAINER_VIEWS else DEFAULT_CONTAINER_VIEW


def resolve_parent(config, module_file):
    """Bind a testlist to the container declared in its own directory.

    The child names its parent and the directory's stub names itself; they
    must agree exactly. 
    """
    dirpath = os.path.dirname(os.path.abspath(module_file))
    parent = load_parent(dirpath)
    if parent is None:
        raise ParentError(
            f"{dirpath} has no {TESTPARENT_FILE}. Every testlist belongs to a "
            f"container; create the stub declaring this directory's name, "
            f"archtype and platform.")

    declared = config.get("parent")
    if not declared:
        raise ParentError(
            f"CONFIG has no 'parent'. Declare it as the container this test "
            f"belongs to: \"parent\": \"{parent['name']}\".")
    if declared != parent["name"]:
        raise ParentError(
            f"CONFIG parent {declared!r} does not match {dirpath}/"
            f"{TESTPARENT_FILE}, which declares {parent['name']!r}.")

    if not config.get("function"):
        raise ParentError(
            "CONFIG has no 'function'. It is this test's button label under "
            "its parent (e.g. build, run, dosbox, 86box).")
    return parent


_URL_RE = re.compile(r'(https?://[^\s<>"]+)')
_URL_TRAILING_PUNCT = '.,;:!?)]}\'"'


def _linkify_line(text):
    """Escape a single line and turn any http(s) URL in it into a link.

    Returns a plain str (not Markup) so callers can compose it further.
    """
    parts = _URL_RE.split(text)
    html_parts = []
    for part in parts:
        if not _URL_RE.match(part):
            html_parts.append(str(escape(part)))
            continue
        url = part.rstrip(_URL_TRAILING_PUNCT)
        trailing = part[len(url):]
        html_parts.append(
            f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">{escape(url)}</a>'
            f'{escape(trailing)}'
        )
    return "".join(html_parts)


def linkify(text):
    """Escape text and turn any http(s) URL in it into a clickable link.

    Used for container descriptions in __testparent__.py
    """
    if not text:
        return Markup("")
    return Markup(_linkify_line(str(text)))


def render_description(text):
    """Render a __testparent__.py description as basic markdown.

    Supports what container descriptions actually use: blank-line-separated
    paragraphs, single newlines as line breaks within a paragraph, and
    "- " prefixed lines as a bullet list. Everything else is escaped and
    URLs are still linkified, same as `linkify`.
    """
    if not text:
        return Markup("")

    html_parts = []
    list_open = False
    para_lines = []

    def flush_para():
        if para_lines:
            html_parts.append(
                "<p>" + "<br>".join(_linkify_line(l) for l in para_lines) + "</p>"
            )
            para_lines.clear()

    for line in str(text).split("\n"):
        stripped = line.strip()
        if stripped.startswith("- "):
            flush_para()
            if not list_open:
                html_parts.append("<ul>")
                list_open = True
            html_parts.append(f"<li>{_linkify_line(stripped[2:])}</li>")
            continue
        if list_open:
            html_parts.append("</ul>")
            list_open = False
        if stripped == "":
            flush_para()
            continue
        para_lines.append(stripped)

    flush_para()
    if list_open:
        html_parts.append("</ul>")

    return Markup("".join(html_parts))


def project_relpath(meta):
    """Directory a registered testlist lives in, relative to /testsrc.

    e.g. {"__full_path__": "/testsrc/sourcedir/OWC_CLONETEST2/__testlist__..."}
    -> "sourcedir/OWC_CLONETEST2". 
     
    obtained from test_runner.reload_tests()'s filesystem walk
     
    Returns None if the testlist hasn't been discovered on disk
    (registry entry has no __full_path__, e.g. a stale/removed module).
    """
    full_path = (meta or {}).get("__full_path__")
    if not full_path:
        return None
    return os.path.relpath(os.path.dirname(full_path), TESTSRC_BASE)


def clear_registries():
    with registry_lock:
        for r in list(registry_map.values()):
            r[:] = []
        testfile_registry.clear()
        # Stubs are re-read on the next reload, so edits to a __testparent__.py
        # take effect without restarting the app. Same for a container's
        # __testplan__.py: a template edit must reach every testlist that
        # imports it on the next reload, not on the next restart.
        _parent_cache.clear()
        try:
            import stepplan
            stepplan.clear_plan_cache()
        except ImportError:
            # apphelpers is also imported by testlists running outside the
            # app, where stepplan is not on the path and nothing is cached.
            pass


def _add_to_registry(registry, description, func):
    func.test_description = description
    if func not in registry:
        registry.append(func)
    return func


def register_testfile(id, types, description=None, tags=None, system=None, platform=None,
                       batch_exclude=False, is_tool=False, build_tool=False):
    def decorator(module):
        modname = module.__name__
        types_dict = {t: modname for t in types} if isinstance(types, list) else types

        with registry_lock:
            testfile_registry[modname] = {
                "id": id,
                "types": types_dict,
                "description": description,
                "tags": tags or [],
                "system": system,
                "platform": platform,
                "batch_exclude": bool(batch_exclude),
                # Infrastructure testlists (build/deploy) that prepare a
                # shared tree rather than exercise it. Kept out of "Run
                # Tests" so a routine test pass never re-triggers a rebuild
                # or a deploy; they only run via "Run Tools" (or by hand).
                "is_tool": bool(is_tool),
                # Sub-kind of is_tool: a build_tool compiles from a
                # configurable input (e.g. a git ref) and so can be paired
                # A/B like a variant is -- "Build A"/"Build B" run it with
                # slot A/B's input overrides and register the result straight
                # into that slot. A plain is_tool (host_tool: deploy steps,
                # tools with no varying input) stays mono: one Run button,
                # no slot pairing. See buildhelper.py.
                "build_tool": bool(build_tool),
            }
        return module
    return decorator


def registry_warnings():
    """Detect provable identity collisions across the registry.

    Both kinds here are decidable from the data:

    - duplicate_parent: two directories declare the same container name+arch
      in their __testparent__.py
    - duplicate_testtype: two files share the same testname+system+testtype,
      status history is keyed by testname+testtype in the reportDB collision 
    """
    warnings = []
    by_container = {}
    with registry_lock:
        for modname, info in testfile_registry.items():
            path = project_relpath(info)
            if not path:
                continue
            by_container.setdefault((info["id"], info.get("system")), set()).add(path)
        for (name, system), paths in sorted(by_container.items(), key=lambda kv: str(kv[0])):
            if len(paths) > 1:
                warnings.append({
                    "kind": "duplicate_parent",
                    "testname": name,
                    "system": system,
                    "paths": sorted(paths),
                    "message": f"{name} declared by {len(paths)} directories: "
                               f"{', '.join(sorted(paths))}",
                })

        seen_variant = {}
        for modname, info in testfile_registry.items():
            for testtype in (info.get("types") or {}):
                key = (info["id"], info.get("system"), testtype)
                if key in seen_variant:
                    other = seen_variant[key]
                    warnings.append({
                        "kind": "duplicate_testtype",
                        "module": modname,
                        "conflicts_with": other,
                        "testname": info["id"],
                        "testtype": testtype,
                        "message": f"{modname} ({project_relpath(info)})",
                    })
                else:
                    seen_variant[key] = modname

    return warnings


def register_test(test_type, desc):
    if test_type not in registry_map:
        registry_map[test_type] = []  # dynamically create a registry for unknown types
    return lambda f: _add_to_registry(registry_map[test_type], desc, f)


def register_mytest(testtype, step_name):
    global _step_counter
    _step_counter += 1
    desc = f"{testtype} {_step_counter} - {step_name}"
    return register_test(testtype, desc)


def reset_step_counter():
    global _step_counter
    _step_counter = 0


def build_paths(tree, base_path, config, result=None):
    if result is None:
        result = {}

    rel_raw = tree.get("_rel", "")
    current_rel = rel_raw.format(**config)
    current_full_path = os.path.join(base_path, current_rel)

    for key, value in tree.items():
        if key == "_rel":
            continue
        
        if isinstance(value, dict):
            result[key] = os.path.join(current_full_path, value.get("_rel", "").format(**config))
            build_paths(value, current_full_path, config, result)
        else:
            formatted_val = value.format(**config)
            if key == "linker" and not formatted_val:
                formatted_val = config["projdir"] + "_linker.cfg"
            
            result[key] = os.path.join(current_full_path, formatted_val)

    return result


def init_test_env(config, module_name):
    parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    
    from apphelpers import register_testfile

    paths = build_paths(config["structure"], config["projbasedir"], config)
    module = sys.modules[module_name]
    parent = resolve_parent(config, module.__file__)
    # archtype lives in __testparent__.py so every child agrees on it by
    # construction (see TESTPARENT_FILE docstring); dispatch_functions.py's
    # steps read it back out via config.get("archtype") at run time, so it
    # has to land in CONFIG here rather than only feeding registration below.
    config["archtype"] = parent["archtype"]

    register_testfile(
        id=parent["name"],
        types=[config["function"]],
        # Optional free-prose "what is this test for" from CONFIG. Absent on
        # every testlist written before the key existed, hence the default.
        description=config.get("description"),
        # Searchable labels ("floppy", "sound", "boot") -- absent on every
        # testlist written before the key existed, same as description.
        tags=config.get("tags"),
        system=parent["archtype"].upper(),
        platform=parent["platform"],
        # Manual-only tests (e.g. long-running toolchain builds) opt out of
        # "run all" batching -- they still show up individually and count
        # for "run failed".
        batch_exclude=config.get("batch_exclude", False),
        is_tool=config.get("is_tool", False),
        build_tool=config.get("build_tool", False),
    )(module)
    reset_step_counter()
    return paths







# path view functions new featuer
def resolve_meta(data, context):
    if isinstance(data, str):
        try:
            # Iteratively resolve in case of nested placeholders
            resolved = data
            for _ in range(3):
                new_resolved = resolved.format(**context)
                if new_resolved == resolved:
                    break
                resolved = new_resolved
            return resolved
        except (KeyError, IndexError, ValueError):
            # Leave anything .format() can't parse
            # as a real template exactly as it was.
            return data
    elif isinstance(data, list):
        return [resolve_meta(i, context) for i in data]
    elif isinstance(data, dict):
        return {k: resolve_meta(v, context) for k, v in data.items()}
    return data

def get_directory_state(base_path):
    files_found = []
    if not os.path.exists(base_path):
        return None
    
    for root, dirs, files in os.walk(base_path):
        for file in files:
            full_path = os.path.join(root, file)
            # Relative to the project dir for easier comparison
            rel_path = os.path.relpath(full_path, base_path)
            files_found.append(rel_path)
    return files_found

def process_config(config_dict):
    # Resolve all {placeholders} using the top-level keys as context
    resolved = resolve_meta(config_dict, config_dict)
    
    proj_path = os.path.join(resolved['projbasedir'], resolved['projdir'])
    
    print(f"Checking Path: {proj_path}")
    
    actual_files = get_directory_state(proj_path)
    
    return {
        "resolved_config": resolved,
        "actual_files": actual_files,
        "project_root": proj_path
    }


TESTLIST_MARKER = "__testlist__"
SEPARATOR = "."


def _kebab(text):
    text = (text or "").strip().lower()
    text = re.sub(r"[\s_./]+", "-", text)
    text = re.sub(r"[^a-z0-9\-]", "", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")


def _split(modname):
    parts = modname.split(".")
    proj = parts[0] if len(parts) > 1 else ""
    leaf = parts[-1]
    if leaf.startswith(TESTLIST_MARKER):
        leaf = leaf[len(TESTLIST_MARKER):]
    return proj, leaf


def _candidates(modname):
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
    modnames = sorted(modnames)
    wanted = {m: _candidates(m) for m in modnames}

    counts = {}
    for m in modnames:
        counts[wanted[m][0]] = counts.get(wanted[m][0], 0) + 1

    slug_to_mod, mod_to_slug = {}, {}
    for m in modnames:
        options = wanted[m]
        chosen = options[0] if counts[options[0]] == 1 else options[-1]
        base, n = chosen, 2
        while chosen in slug_to_mod:
            chosen = f"{base}-{n}"
            n += 1
        slug_to_mod[chosen] = m
        mod_to_slug[m] = chosen
    return slug_to_mod, mod_to_slug


def slug_for(modname, modnames):
    _, mod_to_slug = build_index(modnames)
    return mod_to_slug.get(modname, modname)


def resolve(test_id, modnames):
    modnames = list(modnames)
    if test_id in modnames:
        return test_id
    slug_to_mod, _ = build_index(modnames)
    return slug_to_mod.get(test_id)


def container_id(name, system):
    return f"{_kebab(name)}--{_kebab(system)}"


def resolve_container(cid, registry):
    return sorted(m for m, info in registry.items()
                  if container_id(info.get("id"), info.get("system")) == cid)