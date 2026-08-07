class FixAgentError(Exception):
    """Expected configuration, contract, or job processing failure."""


class EnvironmentSetupError(FixAgentError):
    """Target repository setup failed before its harness could run."""
