"""Local identity-directory login-name rule upgrade tool.

Only the mapping *login name -> immutable account_id* is managed here.
Credentials are assumed to be bound to account_id elsewhere; this package
never validates passwords or talks to external services.

Public entry points:

* :mod:`solution.normalization` -- V1/V2 login name normalization
* :mod:`solution.directory`     -- tenant/account directory operations
* :mod:`solution.plan`          -- precheck reports, migration plans, commits
* ``python -m solution``        -- JSON case runner CLI
"""

__all__ = ["errors", "normalization", "directory", "plan", "canonical", "db"]
