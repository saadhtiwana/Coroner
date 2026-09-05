"""Coroner brain: reasoning over a collected evidence contract.

The brain holds no cluster credentials. It receives an evidence contract, emits
a diagnosis with cited evidence, and never touches the cluster itself. See
docs/DESIGN.md section 1.
"""

__version__ = "0.1.0"

# Schema version of the evidence contract this service understands. Must track
# the Go agent's contract.Version constant.
CONTRACT_VERSION = "1"
