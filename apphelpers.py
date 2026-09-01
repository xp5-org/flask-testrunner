import sys, os
import re
import importlib.util
import os
import inspect
from markupsafe import escape, Markup
_step_counter = 0

testfile_registry = {}
registry_map = {}


helperdir = "/testsrc/pyhelpers"
TESTSRC_BASE = "/testsrc"

TESTPARENT_FILE = "__testparent__.py"
REQUIRED_PARENT_KEYS = ("name", "archtype", "platform")

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


def linkify(text):
    """Escape text and turn any http(s) URL in it into a clickable link.

    Used for container descriptions in __testparent__.py
    """
    if not text:
        return Markup("")
    parts = _URL_RE.split(str(text))
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
    for r in list(registry_map.values()):
        r[:] = []
    testfile_registry.clear()
    # Stubs are re-read on the next reload, so edits to a __testparent__.py
    # take effect without restarting the app.
    _parent_cache.clear()


def _add_to_registry(registry, description, func):
    func.test_description = description
    if func not in registry:
        registry.append(func)
    return func


def register_testfile(id, types, description=None, tags=None, system=None, platform=None):
    def decorator(module):
        modname = module.__name__
        types_dict = {t: modname for t in types} if isinstance(types, list) else types
        
        testfile_registry[modname] = {
            "id": id,
            "types": types_dict,
            "description": description,
            "tags": tags or [],
            "system": system,
            "platform": platform,
        }
        return module
    return decorator


def registry_warnings():
    """Detect provable identity collisions across the registry.

    Both kinds here are decidable from the data:

    - duplicate_parent: two directories declare the same container name+arch
      in their __testparent__.py, so two separate containers render as one row.
    - duplicate_testtype: two files share the same testname+system+testtype,
      so their run buttons carry the same label and their status history
      (keyed by testname+testtype in the report DB) collides. That is a
      demonstrable clash -- two testlists cannot both own one key.
    """
    warnings = []

    # Two directories claiming one container name+arch merge into a single row
    # even though they are separate containers -- provable from the stubs, no
    # inference: each directory states its own name, and two states either
    # match or they don't.
    by_container = {}
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
        except KeyError:
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