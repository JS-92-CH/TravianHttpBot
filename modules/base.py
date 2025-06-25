class BaseModule:
    """Base class for all village modules."""

    def __init__(self, agent):
        self.agent = agent

    def tick(self, village_data):
        """Run one iteration of the module."""
        pass
