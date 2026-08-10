class FixAgentError(Exception):
    """Expected configuration, contract, or job processing failure."""


class EnvironmentSetupError(FixAgentError):
    """Target repository setup failed before its harness could run."""


class JobTerminalError(FixAgentError):
    """A retry cannot make progress without new review data or operator action."""


class JobTimeBudgetExceeded(JobTerminalError):
    """The cumulative execution time for one job or batch was exhausted."""
