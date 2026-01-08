class ProgressState:
    def __init__(self):
        self.step = "Idle"
        self.test_name = ""

# Single instance shared by everyone
state = ProgressState()