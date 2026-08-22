import sys, os
import importlib.util
import os
import inspect
import difflib
import itertools
_step_counter = 0

testfile_registry = {}
registry_map = {}


helperdir = "/testsrc/pyhelpers"
TESTSRC_BASE = "/testsrc"


def project_relpath(meta):
    """Directory a registered testlist lives in, relative to /testsrc.

    e.g. {"__full_path__": "/testsrc/sourcedir/OWC_CLONETEST2/__testlist__..."}
    -> "sourcedir/OWC_CLONETEST2". This is the path a human actually edits --
    unlike the URL slug (lowercased/hyphenated, lossy) or testname (free text,
    not required to match the directory), it's derived straight from
    test_runner.reload_tests()'s filesystem walk, so it's exact by
    construction. Returns None if the testlist hasn't been discovered on disk
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
    """Detect likely testlist authoring mistakes across the registry.

    Two classes of bug this catches, both seen in practice from a copy-pasted
    __testlist__*.py that wasn't fully edited:

    - testname_mismatch: a file's testname doesn't match the testname the
      rest of its directory's siblings use, so it silently renders as its
      own duplicate top-level row instead of joining that group (rows are
      grouped by testname+system -- see testid.group_key).
    - duplicate_testtype: two files share the same testname+system+testtype,
      so their run buttons carry the same label and their status history
      (keyed by testname+testtype in the report DB) collides.
    """
    warnings = []

    by_dir = {}
    for modname, info in testfile_registry.items():
        by_dir.setdefault(project_relpath(info), []).append((modname, info))

    # Below what similarity ratio two testnames sharing a directory are
    # treated as "different tests" rather than "one test, mis-copied name".
    # 0.65 clears "OpenWatcom CubeRotate" / "...CubeRotate ModeVal" (0.84) and
    # "m68k retro68 build+run" / "m68k retro68 on basilisk" (0.70), and stays
    # below every unrelated pair in mac_m68k's 5-test directory (max 0.58) --
    # that dir genuinely hosts 5 different tests sharing one source folder,
    # not a copy-paste mistake, so it must not get flagged wholesale.
    SIMILARITY_THRESHOLD = 0.65

    for path, entries in by_dir.items():
        if not path or len(entries) < 2:
            continue
        counts = {}
        for _, info in entries:
            counts[info["id"]] = counts.get(info["id"], 0) + 1
        if len(counts) < 2:
            continue  # every sibling already agrees -- nothing to flag
        majority_name, majority_count = max(counts.items(), key=lambda kv: kv[1])

        if majority_count > 1:
            # Clear majority (e.g. 3 siblings agree, 1 doesn't) -- flag only
            # the outlier(s) relative to it.
            for modname, info in entries:
                if info["id"] != majority_name:
                    warnings.append({
                        "kind": "testname_mismatch",
                        "module": modname,
                        "path": path,
                        "testname": info["id"],
                        "expected_testname": majority_name,
                        "message": f"{modname} ({path})",
                    })
        else:
            # A tie -- every testname in this dir appears exactly once (the
            # common case: two sibling files, two different names). No
            # majority to measure outliers against, and a directory can
            # legitimately host several unrelated tests (mac_m68k has 5), so
            # flag a pair only when the names themselves look like variants
            # of one another, not merely "different".
            flagged = set()
            for (mod_a, info_a), (mod_b, info_b) in itertools.combinations(entries, 2):
                name_a, name_b = info_a["id"], info_b["id"]
                if name_a == name_b:
                    continue
                ratio = difflib.SequenceMatcher(None, name_a.lower(), name_b.lower()).ratio()
                if ratio < SIMILARITY_THRESHOLD:
                    continue
                for modname, info in ((mod_a, info_a), (mod_b, info_b)):
                    if modname in flagged:
                        continue
                    flagged.add(modname)
                    warnings.append({
                        "kind": "testname_mismatch",
                        "module": modname,
                        "path": path,
                        "testname": info["id"],
                        "expected_testname": None,
                        "message": f"{modname} ({path})",
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

    register_testfile(
        id=config.get("testname"),
        types=[config["testtype"]],
        # Optional free-prose "what is this test for" from CONFIG. Absent on
        # every testlist written before the key existed, hence the default.
        description=config.get("description"),
        # Searchable labels ("floppy", "sound", "boot") -- absent on every
        # testlist written before the key existed, same as description.
        tags=config.get("tags"),
        system=config["archtype"].upper(),
        platform=config["platform"],
    )(sys.modules[module_name])
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