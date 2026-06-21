"""Backend-specific database arms behind the shared foundation's seam.

Each backend provides engine wiring and a durability lifecycle. The SQLite arm
(:mod:`aizk.db.backends.sqlite`) is the only implementation today; PostgreSQL is a
planned peer backend that will add its own arm against the same seam.
"""
