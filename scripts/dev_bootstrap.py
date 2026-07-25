"""Idempotently create the disposable C2 PostgreSQL schema from ORM metadata."""

from sqlalchemy import inspect

from app.database.connection import Base, engine
import app.models  # noqa: F401  # register all model tables with Base.metadata


def bootstrap() -> list[str]:
    Base.metadata.create_all(bind=engine, checkfirst=True)
    return sorted(inspect(engine).get_table_names())


if __name__ == "__main__":
    tables = bootstrap()
    print(f"C2 development schema ready ({len(tables)} tables).")
