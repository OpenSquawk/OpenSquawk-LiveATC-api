class StateMachine:
    def __init__(self):
        self.state = None
        self.carry_data = None

    def tick(self):
        if self.state is None:
            return
        next_state = self.state.get_next_state()
        if next_state is None:
            return
        if next_state.evaluate_entry_conditions():
            self.state.on_exit()
            self.state.get_carry_data()
            self.state = next_state
            self.state.on_enter()
