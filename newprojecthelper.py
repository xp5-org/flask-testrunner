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
    if '.' in testname:
        module_name = testname
    else:
        raise ValueError("testname must be a full module name")

    mod = importlib.import_module(module_name)
    source_file = mod.__file__
    dest_dir = os.path.join("/testsrc/sourcedir", outputname)

    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir)

    new_file = os.path.join(dest_dir, f"__testlist__{outputname}.py")


    if not os.path.exists(source_file):
        raise FileNotFoundError(f"Source {source_file} not found")

    shutil.copy2(source_file, new_file)
    update_register_metadata(new_file, outputname)

    print(f"Created at {dest_dir}")




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