"""Process-level service concerns shared by every aizk service.

The package holds what a running process needs before any stage code does
work: the settings read from the process environment, the request trust
boundary, and the FastAPI application factory. It reaches only the database
and the leaf helpers, never a stage.
"""
