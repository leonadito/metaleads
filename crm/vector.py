"""A pgvector column without the pgvector Python package: the value goes as
text ("[0.1,0.2,...]") cast to vector on Postgres, and comes back as text.
On SQLite (tests) it is a plain text column. No fixed dimension, so a change
of embedding model only needs a re-index, not a migration."""
from django.db import models


class VectorField(models.Field):
    description = "pgvector vector"

    def db_type(self, connection):
        return "vector" if connection.vendor == "postgresql" else "text"

    def from_db_value(self, value, expression, connection):
        return self.to_python(value)

    def to_python(self, value):
        if value is None or isinstance(value, list):
            return value
        return [float(x) for x in str(value).strip("[]").split(",") if x.strip()]

    def get_prep_value(self, value):
        if value is None:
            return None
        return "[" + ",".join(repr(float(x)) for x in value) + "]"

    def get_placeholder(self, value, compiler, connection):
        return "%s::vector" if connection.vendor == "postgresql" else "%s"


def literal(vector):
    """The text form of a vector, for raw SQL parameters."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"
