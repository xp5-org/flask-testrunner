class ProgressState:
    def __init__(self):
        self.step = "Idle"
        self.testname = "" # dot path old
        self.testid = "" # new - name with spaces included
        self.testtype = "" # info like build test or test1 test type - for button label
        self.step_name = ""

progress_state = ProgressState()