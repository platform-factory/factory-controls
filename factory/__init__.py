"""factory-controls: the machinery that holds one change class on its rung.

Every module that decides something keeps the decision in a pure function
over plain dicts (GitHub API JSON as returned), with a thin wrapper that does
the reading and writing. The tests call the pure functions with hand-written
JSON, so every rule can be checked offline.
"""
