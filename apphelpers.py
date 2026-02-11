import sys, os
import importlib.util
import os
import inspect
_step_counter = 0

testfile_registry = {}
registry_map = {}


helperdir = "/testsrc/pyhelpers"

def clear_registries():
    for r in list(registry_map.values()):
        r[:] = []
    testfile_registry.clear()


def _add_to_registry(registry, description, func):
    func.test_description = description
    if func not in registry:
        registry.append(func)
    return func


def register_testfile(id, types, description=None, system=None, platform=None):
    def decorator(module):
        modname = module.__name__
        types_dict = {t: modname for t in types} if isinstance(types, list) else types
        
        testfile_registry[modname] = {
            "id": id,
            "types": types_dict,
            "description": description,
            "system": system,
            "platform": platform,
        }
        return module
    return decorator


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


# def init_test_env(config, module_name):
#     import os
#     import sys
    
#     sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

#     if helperdir not in sys.path:
#         sys.path.insert(0, helperdir)
#     from apphelpers import register_testfile, reset_step_counter

#     folder = config.get("projdir", config.get("projname", "")).lstrip('/')
    
#     paths = {}
#     paths["projdir"] = os.path.join(config["projbasedir"], folder)
#     paths["src"] = os.path.join(paths["projdir"], "src")
#     paths["out"] = os.path.join(paths["projdir"], "output")
    
#     paths["d64"] = os.path.join(paths["out"], config["cmainfile"] + ".d64")
#     paths["vice_cfg"] = os.path.join(config["projbasedir"], config["viceconf"])
#     if config.get("linkerconf"):
#         paths["linker"] = os.path.join(paths["projdir"], config["linkerconf"])
#     else:
#         # look for [folder]_linker.cfg inside the project directory
#         paths["linker"] = os.path.join(paths["projdir"], folder + "_linker.cfg")
        
#     paths["cmain_abs"] = os.path.join(paths["src"], config["cmainfile"] + ".c")

#     register_testfile(
#         #id=folder,
#         id=config.get("testname"),
#         types=[config["testtype"]],
#         system=config["archtype"].upper(),
#         platform=config["platform"],
#     )(sys.modules[module_name])
    # reset_step_counter()
    # return paths



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
        system=config["archtype"].upper(),
        platform=config["platform"],
    )(sys.modules[module_name])
    reset_step_counter()
    return paths
