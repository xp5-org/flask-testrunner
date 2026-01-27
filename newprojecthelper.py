import os, shutil
import re
import importlib
import os



#######################################
### config stuff #####################
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, "reports")
FLASKRUNNER_HELPERDIR = "/testrunnerapp/helpers"
TESTSRC_HELPERDIR = "/testsrc/helpers"
TESTSRC_TESTLISTDIR = "/testsrc/sourcedir/projecta/__testlist__mytestlist.py"
DB_PATH = os.path.join(BASE_DIR, "report.sqlite")
#######################################



def newprojdir(outputname):
    full_path = os.path.join("/testsrc/sourcedir", outputname)
    if not os.path.exists(full_path):
        os.makedirs(full_path)
    return full_path


def copybuildtest(src_dir, outputname, testlist_name=None, dest_dir=None,
                  cmainfile=None, testtype=None, archtype=None,
                  platform=None, viceconf=None, linkerconf=None):
    import os
    import shutil

    if not os.path.isdir(src_dir):
        raise ValueError(f"Source directory does not exist: {src_dir}")

    if dest_dir is None:
        parts = src_dir.split(os.sep)
        try:
            platform_index = parts.index("sourcedir") + 1
            platform_name = parts[platform_index]
        except (ValueError, IndexError):
            raise RuntimeError(f"Cannot determine platform from {src_dir}")
        dest_dir = os.path.join("/testsrc/sourcedir", platform_name, outputname)

    dest_dir = os.path.abspath(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)

    for fname in os.listdir(src_dir):
        src_path = os.path.join(src_dir, fname)
        if os.path.isfile(src_path):
            if fname.startswith("__testlist__"):
                testlist_file = testlist_name or "__testlist__{}".format(outputname)
                dst_path = os.path.join(dest_dir, "{}.py".format(testlist_file))
            else:
                dst_path = os.path.join(dest_dir, fname)

            if os.path.abspath(src_path) == os.path.abspath(dst_path):
                continue

            shutil.copy2(src_path, dst_path)

    testlist_file = testlist_name or "__testlist__{}".format(outputname)
    update_register_metadata(
        os.path.join(dest_dir, "{}.py".format(testlist_file)),
        new_projname=outputname,
        new_projdirname=os.path.basename(dest_dir.rstrip('/')),
        new_cmainfile=cmainfile,
        new_testtype=testtype,
        new_archtype=archtype,
        new_platform=platform,
        new_viceconf=viceconf,
        new_linkerconf=linkerconf
    )

    return dest_dir, "{}.py".format(testlist_file)


def copy_sourcedir(src_path, dst_path):
    if os.path.exists(dst_path):
        shutil.rmtree(dst_path)
    shutil.copytree(src_path, dst_path)




def update_register_metadata(pyfile_path, new_projname=None, new_projdirname=None, new_cmainfile=None,
                             new_testtype=None, new_archtype=None, new_platform=None,
                             new_viceconf=None, new_linkerconf=None):
    with open(pyfile_path, "r", encoding="utf-8") as f:
        content = f.read()



    # mapping of CONFIG keys to new values
    updates = {
        "projname": new_projname,
        "projdirname": new_projdirname,
        "cmainfile": new_cmainfile,
        "testtype": new_testtype,
        "archtype": new_archtype,
        "platform": new_platform,
        "viceconf": new_viceconf,
        "linkerconf": new_linkerconf,
    }

    # print("update to file conf called: ", updates)

    for key, new_val in updates.items():
        if new_val is not None:
            # regex matches: "key": "value" (with optional whitespace)
            pattern = rf'("{key}"\s*:\s*)["\'].*?["\']'
            replacement = rf'\1"{new_val}"'
            content = re.sub(pattern, replacement, content)

    with open(pyfile_path, "w", encoding="utf-8") as f:
        f.write(content)




basic_lines = []