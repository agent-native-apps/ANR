"""Live visualizer for the agent-native runtime.

Decoupled from the runtime: reads the declarative spec for structure and
tails the mesh's JSONL audit log for activity. Renders the live graph as
SVG with CSS animations, served by a tiny FastAPI app. The runtime does
not know the visualizer exists; the contract between them is the shape
of the audit log and the spec.
"""

__version__ = "0.1.0"
