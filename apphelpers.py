

PROGRESS_FILE = "progress.txt"
REPORT_DIR = "reports"
compile_logs_dir = "compile_logs"

testfile_registry = {}
buildtest_registry = []
playtest_registry = []
packagetest_registry = []

registry_map = {
    "build": buildtest_registry,
    "play": playtest_registry,
    "package": packagetest_registry,
}

def clear_registries():
    for r in registry_map.values():
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

def register_playtest(desc):
    return lambda f: _add_to_registry(playtest_registry, desc, f)

def register_buildtest(desc):
    return lambda f: _add_to_registry(buildtest_registry, desc, f)

def register_packagetest(desc):
    return lambda f: _add_to_registry(packagetest_registry, desc, f)