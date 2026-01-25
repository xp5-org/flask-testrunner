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

def copybuildtest(src_dir, outputname):
    import os
    import shutil

    if not os.path.isdir(src_dir):
        raise ValueError(f"Source directory does not exist: {src_dir}")

    # Assume platform is the folder immediately under sourcedir
    parts = src_dir.split(os.sep)
    try:
        platform_index = parts.index("sourcedir") + 1
        platform = parts[platform_index]
    except (ValueError, IndexError):
        raise RuntimeError(f"Cannot determine platform from {src_dir}")

    dest_dir = os.path.join("/testsrc/sourcedir", platform, outputname)
    os.makedirs(dest_dir, exist_ok=True)

    # Copy all files, rename main test file if needed
    for fname in os.listdir(src_dir):
        src_path = os.path.join(src_dir, fname)
        if os.path.isfile(src_path):
            if fname.startswith("__testlist__"):
                dst_path = os.path.join(dest_dir, f"__testlist__{outputname}.py")
            else:
                dst_path = os.path.join(dest_dir, fname)
            shutil.copy2(src_path, dst_path)

    # Optionally update metadata if you have that function
    update_register_metadata(os.path.join(dest_dir, f"__testlist__{outputname}.py"), outputname)

    print(f"Copied {src_dir} → {dest_dir}")






def update_register_metadata(pyfile_path, new_id=None, new_platform=None):
    with open(pyfile_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Update id="..."
    if new_id:
        content = re.sub(r'id\s*=\s*["\'].*?["\']', f'id="{new_id}"', content)

    # Update platform="..."
    if new_platform:
        content = re.sub(r'platform\s*=\s*["\'].*?["\']', f'platform="{new_platform}"', content)

    with open(pyfile_path, "w", encoding="utf-8") as f:
        f.write(content)




basic_lines = []