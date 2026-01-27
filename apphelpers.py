

PROGRESS_FILE = "progress.txt"
REPORT_DIR = "reports"
compile_logs_dir = "compile_logs"

_step_counter = 0

testfile_registry = {}
buildtest_registry = []
playtest_registry = []
packagetest_registry = []

registry_map = {
    "build": buildtest_registry,
    "play": playtest_registry,
    "package": packagetest_registry,
}

# def clear_registries():
#     for r in registry_map.values():
#         r[:] = []
#     testfile_registry.clear()

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


register_playtest = lambda desc: register_test("play", desc)
register_buildtest = lambda desc: register_test("build", desc)
register_packagetest = lambda desc: register_test("package", desc)



def register_mytest(testtype, step_name):
    global _step_counter
    _step_counter += 1
    desc = f"{testtype} {_step_counter} - {step_name}"
    return register_test(testtype, desc)


def reset_step_counter():
    global _step_counter
    _step_counter = 0