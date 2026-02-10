# this is in its own file so it can be imported easily

class ProgressState:
    def __init__(self):
        self.step = "Idle"
        self.testname = "" # dot path old
        self.testid = "" # new - name with spaces included
        self.testtype = "" # info like build test or test1 test type - for button label
        self.step_name = ""

progress_state = ProgressState()

# nav bar button builder
def build_nav(app):
    items = []
    for endpoint, view in app.view_functions.items():
        label = getattr(view, "nav_label", None)
        if label:
            items.append({"name": label, "endpoint": endpoint})
    return items

def nav(label):
    def decorator(f):
        f.nav_label = label
        return f
    return decorator