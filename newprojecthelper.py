import os
import shutil
import re
import importlib

#######################################
### config stuff #####################
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, "reports")
FLASKRUNNER_HELPERDIR = "/testrunnerapp/helpers"
TESTSRC_HELPERDIR = "/testsrc/helpers"
TESTSRC_TESTLISTDIR = "/testsrc/sourcedir/projecta/__testlist__mytestlist.py"
DB_PATH = os.path.join(BASE_DIR, "report.sqlite")
#######################################
basic_lines = []




def newprojdir(outputname):
    full_path = os.path.join("/testsrc/sourcedir", outputname)
    if not os.path.exists(full_path):
        os.makedirs(full_path)
    return full_path


def copybuildtest(src_dir, outputname, testlist_name=None, dest_dir=None, **kwargs):
    # does way more than copy build test, need to rename or separate out
    import os
    import shutil

    if not os.path.isdir(src_dir):
        raise ValueError(f"Source directory does not exist: {src_dir}")

    if dest_dir is None:
        parts = src_dir.split(os.sep)
        try:
            platform_index = parts.index("sourcedir") + 1
            platform_name = parts[platform_index]
            dest_dir = os.path.join("/testsrc/sourcedir", platform_name, outputname)
        except (ValueError, IndexError):
            dest_dir = os.path.join(src_dir, "..", outputname)

    src_dir_abs = os.path.abspath(src_dir)
    dest_dir_abs = os.path.abspath(dest_dir)
    final_testlist_name = "{}.py".format(testlist_name) if testlist_name else "__testlist__{}.py".format(outputname)
    target_testlist_path = os.path.join(dest_dir_abs, final_testlist_name)

    if src_dir_abs != dest_dir_abs:
        os.makedirs(dest_dir_abs, exist_ok=True)
        src_code_folder = os.path.join(src_dir_abs, 'src')
        dst_code_folder = os.path.join(dest_dir_abs, 'src')
        
        if os.path.exists(src_code_folder):
            copy_sourcedir(src_code_folder, dst_code_folder)

        source_testlist_path = None
        for fname in os.listdir(src_dir_abs):
            if fname.startswith("__testlist__") and fname.endswith(".py"):
                source_testlist_path = os.path.join(src_dir_abs, fname)
                break
        
        if source_testlist_path:
            shutil.copy2(source_testlist_path, target_testlist_path)
            if 'projdir' not in kwargs:
                kwargs['projdir'] = os.path.basename(dest_dir_abs.rstrip('/'))
                
            update_register_metadata(target_testlist_path, **kwargs)

    return dest_dir_abs, final_testlist_name


def copy_sourcedir(src_path, dst_path):
    if os.path.exists(dst_path):
        shutil.rmtree(dst_path)
    shutil.copytree(src_path, dst_path)


def update_register_metadata(pyfile_path, **kwargs):
    with open(pyfile_path, "r", encoding="utf-8") as f:
        content = f.read()

    for key, new_val in kwargs.items():
        if new_val is not None:
            # match single or double quotes as key. 
            # parse : separator
            # replaces 'val' or "val"
            pattern = rf'([\'"]{key}[\'"]\s*:\s*)([\'"].*?[\'"])'
            replacement = rf'\1"{new_val}"'
            
            content = re.sub(pattern, replacement, content)

    with open(pyfile_path, "w", encoding="utf-8") as f:
        f.write(content)