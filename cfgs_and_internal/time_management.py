import time
from utils import load_toml_as_dict


class TimeManagement:
    def __init__(self):
        self.thresholds = load_toml_as_dict("cfg/time_tresholds.toml")
        self.states = {key: time.time() for key in self.thresholds.keys()}

    def start(self):
        current_time = time.time()
        self.states = {key: current_time for key in self.thresholds}

    def check_time(self, check_type):
        current_time = time.time()
        if (current_time - self.states[check_type]) >= self.thresholds[check_type]:
            self.states[check_type] = current_time  # Reset the timer right after checking
            return True
        return False

    def state_check(self):
        return self.check_time('state_check')

    def no_detections_check(self):
        return self.check_time('no_detections')

    def idle_check(self):
        return self.check_time("idle")
