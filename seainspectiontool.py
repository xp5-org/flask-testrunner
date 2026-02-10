import re
from collections import defaultdict

def parse_c_file(filename):
    includes = set()
    functions = {}

    with open(filename, "r") as f:
        code = f.read()

    # Remove comments
    code = re.sub(r'//.*', '', code)
    code = re.sub(r'/\*.*?\*/', '', code, flags=re.DOTALL)

    # Extract includes
    includes.update(re.findall(r'#include\s*<([^>]+)>', code))

    # Match function definitions: return_type name(args) { body }
    func_body_matches = re.findall(r'\b(\w+)\s+(\w+)\s*\(([^)]*)\)\s*{([^}]*)}', code, flags=re.DOTALL)

    for ret_type, func_name, args, body in func_body_matches:
        arg_count = 0 if args.strip() in ("", "void") else len([a for a in args.split(',') if a.strip()])
        functions.setdefault(func_name, {"args": arg_count, "calls": []})

        # Extract function calls inside the body
        for call, call_args in re.findall(r'\b(\w+)\s*\(([^;{}]*)\)', body):
            call_arg_count = 0 if call_args.strip() == "" else len([a for a in call_args.split(',') if a.strip()])
            functions.setdefault(call, {"args": call_arg_count, "calls": []})
            functions[func_name]["calls"].append(call)

    return {"includes": includes, "functions": functions}

def print_call_tree(functions, func_name, indent=0, visited=None):
    if visited is None:
        visited = set()
    args = functions.get(func_name, {}).get("args", "?")
    print("    " * indent + f"{func_name}({args} args)")
    if func_name in visited:
        print("    " * (indent + 1) + "(recursive)")
        return
    visited.add(func_name)
    for called in functions[func_name]["calls"]:
        if called in functions:
            print_call_tree(functions, called, indent + 1, visited.copy())
        else:
            # For unknown functions, we don't know arg count
            print("    " * (indent + 1) + f"{called}(?)")


if __name__ == "__main__":
    result = parse_c_file("/testsrc/sourcedir/c64src/cadrender_copy2/src/test1.c")
    print("Includes:", result["includes"])
    print("\nFunction call tree:")
    for func in result["functions"]:
        # Only print top-level functions (you can filter if you want)
        print_call_tree(result["functions"], func)
