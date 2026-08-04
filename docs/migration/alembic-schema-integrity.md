# Alembic schema integrity

The initial revision is explicit, frozen DDL. Runtime schema initialization and
storage integration tests execute `alembic upgrade head`; they do not call
`Base.metadata.create_all()`.

CI upgrades an empty SQLite database and then runs `alembic check`. A model
change without a matching revision therefore fails before merge. PostgreSQL
integration will run the same revision chain when that CI service is enabled.
