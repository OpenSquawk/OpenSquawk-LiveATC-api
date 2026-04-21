class State:
    def __init__(self, name: str, code: str):
        self.name = name
        self.code = code

    def get_next_state(self) -> 'State' | None:
        return None

    def evaluate_entry_conditions(self) -> bool:
        return True

    def on_enter(self):
        pass

    def on_exit(self):
        pass
