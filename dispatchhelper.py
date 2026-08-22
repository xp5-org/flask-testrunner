import sys
import os
import importlib.util
import inspect
import traceback

if "/" not in sys.path:
    sys.path.insert(0, "/")

PROJECT_STEP_DISPATCH = {}
PROJECT_STEP_SCHEMAS = {}


def _build_arg_schema(func):
    sig = inspect.signature(func)
    return {
        k: v.default if v.default is not inspect.Parameter.empty else ""
        for k, v in sig.parameters.items()
        if k not in ['kwargs', 'context', 'config']
    }


def _safe_wrap(func):
    # catch unhandled exceptions
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            tb = traceback.format_exc()
            msg = (
                f"DISPATCH ERROR in '{func.__name__}':\n"
                f"{type(e).__name__}: {e}\n\n"
                f"{tb}"
            )
            print(f"[dispatchhelper] {msg}")
            return False, msg

    # Copy any custom attributes the original function carries
    # (e.g. _is_teststep, test_description, my_test_type)
    for attr in vars(func):
        setattr(wrapper, attr, getattr(func, attr))

    return wrapper


def load_step_dispatch(project_path, force_reload=False):
    global PROJECT_STEP_DISPATCH, PROJECT_STEP_SCHEMAS

    print(f"\n[DEBUG] Attempting to load dispatch from: {project_path}")

    if project_path in PROJECT_STEP_DISPATCH and not force_reload:
        print(f"[DEBUG] Using cached dispatch for {project_path}")
        return PROJECT_STEP_DISPATCH[project_path]

    helper_file = os.path.join(project_path, "dispatch_functions.py")
    print(f"[DEBUG] Looking for helper file: {helper_file}")

    if not os.path.exists(helper_file):
        print(f"[DEBUG] ERROR: {helper_file} does not exist!")
        PROJECT_STEP_DISPATCH[project_path] = {}
        PROJECT_STEP_SCHEMAS[project_path] = {}
        return {}

    try:
        spec = importlib.util.spec_from_file_location(
            "project_helpers_" + os.path.basename(project_path),
            helper_file
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        print(f"[DEBUG] Successfully imported module: {module.__name__}")
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[DEBUG] EXCEPTION during module load: {e}\n{tb}")
        return {}

    dispatch = {}
    for name, obj in inspect.getmembers(module):
        if inspect.isfunction(obj) and not name.startswith("_"):
            dispatch[name] = _safe_wrap(obj)

    print(f"[DEBUG] Functions found in {os.path.basename(helper_file)}: {list(dispatch.keys())}")

    schemas = {}
    for key, func in dispatch.items():
        schemas[key] = _build_arg_schema(func)

    PROJECT_STEP_DISPATCH[project_path] = dispatch
    PROJECT_STEP_SCHEMAS[project_path] = schemas
    return dispatch


def get_step_dispatch(project_path):
    return PROJECT_STEP_DISPATCH.get(project_path, {})


def get_step_schema(project_path):
    return PROJECT_STEP_SCHEMAS.get(project_path, {})