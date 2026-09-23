"""claude-code-multi-model: Claude Code on your own Claude subscription, with
subagents served by any other model through a loopback gateway.

Two modules do the work:

* :mod:`claude_code_multi_model.gateway` is the sidecar Claude Code points at.
  ``claude-*`` requests go to Anthropic with the credential Claude Code
  already holds; every other model name goes to the upstream you chose.
* :mod:`claude_code_multi_model.passthrough` is the Anthropic relay: byte-for-
  byte, streamed, with the client's own credential and nothing else.

:mod:`claude_code_multi_model.cli` is the ``claude-multi`` launcher.
"""

__version__ = "0.1.0"
