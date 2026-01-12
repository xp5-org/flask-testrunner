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


def copybuildtest(testname, outputname):
    if '.' not in testname:
        raise ValueError("testname must be a full module name")

    mod = importlib.import_module(testname)
    source_file = mod.__file__
    source_dir = os.path.dirname(source_file)  # parent dir of the test

    # Extract platform (assumes source_dir = /testsrc/sourcedir/<platform>/<project>)
    parts = source_dir.split(os.sep)
    try:
        platform_index = parts.index("sourcedir") + 1
        platform = parts[platform_index]
    except (ValueError, IndexError):
        raise RuntimeError(f"Cannot determine platform from {source_dir}")

    # Build target directory: /testsrc/sourcedir/<platform>/<outputname>
    dest_dir = os.path.join("/testsrc/sourcedir", platform, outputname)
    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir)

    # Copy all files from source_dir to dest_dir
    for fname in os.listdir(source_dir):
        src_path = os.path.join(source_dir, fname)
        if os.path.isfile(src_path):
            # Rename only the test file itself
            if src_path == source_file:
                dst_path = os.path.join(dest_dir, f"__testlist__{outputname}.py")
            else:
                dst_path = os.path.join(dest_dir, fname)
            shutil.copy2(src_path, dst_path)

    update_register_metadata(os.path.join(dest_dir, f"__testlist__{outputname}.py"), outputname)
    print(f"Copied {source_dir} → {dest_dir} (renamed {os.path.basename(source_file)})")






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