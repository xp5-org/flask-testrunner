#this was all generated and needs a new approach to working with the test cloning

"""
newprojecthelper.py
-------------------
Handles cloning an existing testlist project into a new directory.
"""

import os
import ast
import shutil
import re
import json
import importlib.util

import apphelpers
from apphelpers import TESTPARENT_FILE


BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, "reports")

# Project code (qemuhelpers.py, dispatch_functions.py) isn't importable
# normally — it's per-project, loaded fresh from a fixed path the same way
# app.py does. See TESTSRC_HELPERDIR usages in app.py.
TESTSRC_HELPERDIR = "/testsrc/pyhelpers"
TEMPLATES_DIR     = os.path.join(os.path.dirname(TESTSRC_HELPERDIR), "templates")


def _load_qemuhelpers():
    """Load the project's qemuhelpers.py fresh, or None if it isn't there."""
    helper_file = os.path.join(TESTSRC_HELPERDIR, "qemuhelpers.py")
    if not os.path.isfile(helper_file):
        return None
    spec = importlib.util.spec_from_file_location("qemuhelpers_newproj", helper_file)
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _as_bool(v):
    """Coerce a config value (often the string 'True'/'False') to bool —
    mirrors dispatch_functions._as_bool."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _regenerable_disk_names(config):
    """Basenames of disk images the qemu dispatch steps unconditionally
    rebuild every run 

    Skipping the disposable ones (rather than deep-copying) matters because a
    raw copy would carry the SOURCE project's disk writes into the new
    project instead of a clean disk, and for the overlay case specifically
    would silently keep pointing at whatever the source's overlay was backed
    by rather than freshly referencing the shared read-only template.

    Returns {basename: (kind, template_or_None)} where kind is "overlay",
    "flat", or "srcdisk".
    """
    if not config:
        return {}
    names = {}
    overlay = config.get("hdd1_overlay")
    if overlay and config.get("hdd1_template") and not _as_bool(config.get("hdd1_persist")):
        names[os.path.basename(overlay)] = ("overlay", config["hdd1_template"])
    elif config.get("hdd1_qcow") and not config.get("hdd1_template"):
        names[os.path.basename(config["hdd1_qcow"])] = ("flat", None)
    srcdisk = config.get("srcdisk_img")
    if srcdisk:
        names[os.path.basename(srcdisk)] = ("srcdisk", None)
    return names


def _rebuild_overlays(dest_dir_abs, regenerable):
    """Recreate fresh COW overlays in the destination backed shared template"""
    qh = None
    for name, (kind, template) in regenerable.items():
        if kind != "overlay":
            continue
        if qh is None:
            qh = _load_qemuhelpers()
        if qh is None or not hasattr(qh, "create_overlay_image"):
            continue
        template_path = template if os.sep in str(template) else os.path.join(TEMPLATES_DIR, template)
        qh.create_overlay_image(template_path, os.path.join(dest_dir_abs, name), overwrite=True)


# ── Public API ────────────────────────────────────────────────────────────────

def copybuildtest(src_dir, outputname, testlist_name=None, dest_dir=None,
                  repaired_config=None, **kwargs):
    """
    Clone a test project into dest_dir (or a derived path).

    src_dir         — directory containing the source __testlist__ file and src/
    outputname      — new project slug (used as fallback dest folder name)
    testlist_name   — output filename; defaults to __testlist__{outputname}.py
    dest_dir        — absolute destination directory; derived from src_dir if None
    repaired_config — full repaired CONFIG dict (from repair_config.repair_config).
                      When provided, steps AND all scalar values come from here
                      instead of the source file + kwargs regex pass.
    **kwargs        — individual CONFIG key overrides (projdir, testname, etc.)
                      applied after repaired_config if both are given.
    """
    if not os.path.isdir(src_dir):
        raise ValueError(f"Source directory does not exist: {src_dir}")

    # ── Resolve destination ────────────────────────────────────────────────
    if dest_dir is None:
        parts = src_dir.split(os.sep)
        try:
            platform_index = parts.index("sourcedir") + 1
            platform_name  = parts[platform_index]
            dest_dir = os.path.join("/testsrc/sourcedir", platform_name, outputname)
        except (ValueError, IndexError):
            dest_dir = os.path.join(src_dir, "..", outputname)

    src_dir_abs  = os.path.abspath(src_dir)
    dest_dir_abs = os.path.abspath(dest_dir)

    final_testlist_name = (
        "{}.py".format(testlist_name) if testlist_name and not testlist_name.endswith(".py")
        else testlist_name or "__testlist__{}.py".format(outputname)
    )
    target_testlist_path = os.path.join(dest_dir_abs, final_testlist_name)

    if src_dir_abs == dest_dir_abs:
        # Variant clone — same folder, different testlist file. The container
        # is the folder, so the new variant joins the one already declared
        # here; pin its parent to the stub rather than trusting a caller
        # override, which would only ever produce a load error.
        source_testlist_path = _find_testlist(src_dir_abs)
        if source_testlist_path:
            shutil.copy2(source_testlist_path, target_testlist_path)
            existing = _read_parent(src_dir_abs)
            if existing:
                kwargs["parent"] = existing["name"]
            _write_config(target_testlist_path, repaired_config, **kwargs)
        return dest_dir_abs, final_testlist_name

    # ── New-folder clone ───────────────────────────────────────────────────
    os.makedirs(dest_dir_abs, exist_ok=True)

    # Copy ALL project data (src/, disk-image templates like hdd.img /
    # newfloppy.img, config files, etc.) — not just src/ — so a cloned project
    # has everything its steps reference. Skips python caches and testlist
    # files; the chosen testlist is copied under its new name below. Also
    # skips disk images the qemu dispatch steps regenerate every run (COW
    # overlays, the legacy flat conversion qcow2, the D: srcdisk) — see
    # _regenerable_disk_names.
    regenerable = _regenerable_disk_names(repaired_config)
    for entry in os.listdir(src_dir_abs):
        if entry == "__pycache__":
            continue
        if entry.startswith("__testlist__") and entry.endswith(".py"):
            continue
        # The parent stub is per-container, and a new folder is a new
        # container — copying the source's would hand the clone the source's
        # name. A fresh one is written below instead.
        if entry == TESTPARENT_FILE:
            continue
        if entry in regenerable:
            continue
        s = os.path.join(src_dir_abs, entry)
        d = os.path.join(dest_dir_abs, entry)
        if os.path.isdir(s):
            copy_sourcedir(s, d)          # rmtree + copytree (fresh copy)
        else:
            shutil.copy2(s, d)
    _rebuild_overlays(dest_dir_abs, regenerable)

    # Declare the new container. Arch and platform carry over from the source
    # (a clone runs on the same machine); the name is the caller's if it asked
    # for one, else the new project slug.
    src_parent = _read_parent(src_dir_abs) or {}
    requested = kwargs.get("parent") or (repaired_config or {}).get("parent")
    # A new folder is a new container. If the caller didn't rename it, the
    # value carried over from the source CONFIG would name two directories the
    # same thing (a duplicate_parent collision), so fall back to the slug.
    new_parent_name = (requested
                       if requested and requested != src_parent.get("name")
                       else outputname)
    _write_parent(dest_dir_abs, {
        "name": new_parent_name,
        "archtype": src_parent.get("archtype", "unknown"),
        "platform": src_parent.get("platform", ""),
    })

    # Copy testlist file
    source_testlist_path = _find_testlist(src_dir_abs)
    if source_testlist_path:
        shutil.copy2(source_testlist_path, target_testlist_path)
        if "projdir" not in kwargs:
            kwargs["projdir"] = os.path.basename(dest_dir_abs.rstrip("/"))
        kwargs["parent"] = new_parent_name
        _write_config(target_testlist_path, repaired_config, **kwargs)

    return dest_dir_abs, final_testlist_name


def copy_sourcedir(src_path, dst_path):
    if os.path.exists(dst_path):
        shutil.rmtree(dst_path)
    shutil.copytree(src_path, dst_path)


# ── Config writing ────────────────────────────────────────────────────────────

def _find_testlist(directory):
    for fname in os.listdir(directory):
        if fname.startswith("__testlist__") and fname.endswith(".py"):
            return os.path.join(directory, fname)
    return None


def _read_parent(directory):
    """This directory's PARENT dict, or None if it has no stub."""
    try:
        return apphelpers.load_parent(directory)
    except Exception:
        return None


def _write_parent(directory, parent):
    """Write a directory's __testparent__.py."""
    with open(os.path.join(directory, TESTPARENT_FILE), "w", encoding="utf-8") as fh:
        fh.write(
            '"""Parent container for this directory.\n\n'
            'The __testlist__*.py files here declare themselves children of it\n'
            'by name, and each contributes one button. Name/archtype/platform\n'
            'live here so every child agrees on them by construction.\n'
            '"""\n\n'
            "PARENT = {\n"
            f'    "name": {json.dumps(parent["name"])},\n'
            f'    "archtype": {json.dumps(parent["archtype"])},\n'
            f'    "platform": {json.dumps(parent["platform"])},\n'
            "}\n"
        )


def _write_config(pyfile_path, repaired_config=None, **kwargs):
    """
    Write CONFIG changes to a testlist .py file.

    If repaired_config is provided, it is used as the authoritative source for
    all CONFIG values including steps — the full brace-tracking rewriter runs
    over every key in it, then kwargs overrides are applied on top.

    If repaired_config is None, only the kwargs scalar overrides are applied
    (legacy behaviour for simple clones that don't need repair).
    """
    if repaired_config is not None:
        # Build the update map: everything from repaired_config except structure
        # (structure is file-layout metadata, not a runtime value)
        updates = {k: v for k, v in repaired_config.items() if k != "structure"}
        # kwargs take priority over repaired_config
        updates.update({k: v for k, v in kwargs.items()
                        if k not in ("src_dir", "dest_dir", "testlist_name",
                                     "repaired_config", "outputname",
                                     "_repaired_steps")})
        update_config_in_file(pyfile_path, updates)
    else:
        # Legacy: scalar-only update via kwargs
        _update_scalars_only(pyfile_path, kwargs)


def _find_config_dict(tree):
    """The ast.Dict node assigned to CONFIG at module level, or None."""
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "CONFIG" for t in node.targets)
                and isinstance(node.value, ast.Dict)):
            return node.value
    return None


def update_config_in_file(file_path, updates):
    """
    Rewrite CONFIG keys in a testlist .py file.

    Uses the AST to find top-level key's value
    spans, so multi-line values (lists, dicts) are replaced correctly no
    matter what characters their strings contain. Converts Python
    None/True/False properly. Can write any JSON-serialisable value
    including steps lists and None.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        src = f.read()
    lines = src.splitlines(keepends=True)

    tree = ast.parse(src, filename=file_path)
    config = _find_config_dict(tree)
    if config is None:
        raise ValueError(f"No top-level CONFIG = {{...}} dict found in {file_path}")

    entries = []  # (key, value_node) in source order, constant keys only
    for k, v in zip(config.keys, config.values):
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            entries.append((k.value, v))

    written_keys = set()
    skip_until = {}
    replace_at = {}  # 1-indexed start line -> matched update key

    for key, value in entries:
        if key in updates:
            replace_at[value.lineno] = key
            for ln in range(value.lineno, value.end_lineno + 1):
                skip_until[ln] = True

    new_lines = []
    for lineno, line in enumerate(lines, start=1):
        if lineno in replace_at:
            key = replace_at[lineno]
            m = re.match(rf"^(\s*[\"']{re.escape(key)}[\"']\s*:\s*)", line)
            prefix = m.group(1) if m else f'{re.match(r"^\s*", line).group(0)}"{key}": '
            base_indent = re.match(r"^\s*", line).group(0)
            val_str = _py_repr(updates[key], base_indent)
            if not val_str.rstrip().endswith(","):
                val_str = val_str.rstrip() + ","
            new_lines.append(f"{prefix}{val_str}\n")
            written_keys.add(key)
            continue
        if lineno in skip_until:
            continue
        if lineno == config.end_lineno:
            missing = [k for k in updates if k not in written_keys]
            if missing:
                indent = _config_key_indent(lines)
                for j in range(len(new_lines) - 1, -1, -1):
                    stripped = new_lines[j].rstrip()
                    if not stripped or stripped.lstrip().startswith("#"):
                        continue
                    if not stripped.endswith(","):
                        new_lines[j] = stripped + ",\n"
                    break
                for key in missing:
                    val_str = _py_repr(updates[key], indent)
                    new_lines.append(f'{indent}"{key}": {val_str},\n')
        new_lines.append(line)

    with open(file_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def _config_key_indent(lines, default="    "):
    """Indent used by the existing CONFIG keys, so an inserted key lines up."""
    in_config = False
    for line in lines:
        if not in_config:
            if re.search(r"^\s*CONFIG\s*=\s*\{", line):
                in_config = True
            continue
        m = re.match(r"^(\s+)[\"\']", line)
        if m:
            return m.group(1)
    return default


def _py_repr(val, base_indent="    "):
    """Serialise a value to Python source syntax.

    Done structurally rather than by post-processing json.dumps output
    """
    def render(v, indent):
        pad = indent + "    "
        if isinstance(v, bool) or v is None:
            return repr(v)                      # True / False / None
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, str):
            return json.dumps(v)                # double-quoted, escaped
        if isinstance(v, dict):
            if not v:
                return "{}"
            items = [f'{pad}{json.dumps(str(k))}: {render(x, pad)}' for k, x in v.items()]
            return "{\n" + ",\n".join(items) + f"\n{indent}}}"
        if isinstance(v, (list, tuple)):
            if not v:
                return "[]"
            items = [f"{pad}{render(x, pad)}" for x in v]
            return "[\n" + ",\n".join(items) + f"\n{indent}]"
        return json.dumps(v)

    return render(val, base_indent)


def _update_scalars_only(pyfile_path, kwargs):
    """
    Legacy regex-based scalar updater.  Only handles quoted string values.
    Used as fallback when no repaired_config is available.
    """
    with open(pyfile_path, "r", encoding="utf-8") as f:
        content = f.read()
    for key, new_val in kwargs.items():
        if new_val is not None and isinstance(new_val, str):
            pattern     = rf"(['\"]{{key}}['\"]\s*:\s*)(['\"].*?['\"])"
            replacement = rf'\1"{new_val}"'
            content     = re.sub(
                rf"(['\"{key}['\"]\s*:\s*)(['\"].*?['\"])",
                replacement, content
            )
    with open(pyfile_path, "w", encoding="utf-8") as f:
        f.write(content)