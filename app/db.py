"""SQL warehouse access shared by the application routes."""
import os
import json
from collections.abc import Mapping

from databricks import sql


def _server_hostname():
    host = os.environ["DATABRICKS_HOST"]
    return host.removeprefix("https://").removeprefix("http://").rstrip("/")


def _json_value(value):
    """Recursively convert Databricks SQL values into JSON-compatible values."""
    if value is None:
        return None

    if isinstance(value, str):
        text = value.strip()
        if (
            (text.startswith("{") and text.endswith("}"))
            or (text.startswith("[") and text.endswith("]"))
        ):
            try:
                return _json_value(json.loads(text))
            except json.JSONDecodeError:
                pass
        return value
    

    if hasattr(value, "asDict"):
        return {
            key: _json_value(item)
            for key, item in value.asDict(recursive=False).items()
        }

    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]

    return value


def query_rows(query, params=None):
    """Run SQL with optional :named parameters and return JSON-compatible rows."""
    token = os.getenv("DATABRICKS_TOKEN")
    if not token:
        raise RuntimeError(
            "DATABRICKS_TOKEN is not loaded in the current terminal session."
        )

    warehouse_id = os.environ["DATABRICKS_WAREHOUSE_ID"]

    with sql.connect(
        server_hostname=_server_hostname(),
        http_path=f"/sql/1.0/warehouses/{warehouse_id}",
        access_token=token,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, params or {})
            names = [column[0].lower() for column in cursor.description]

            return [
                {
                    name: _json_value(value)
                    for name, value in zip(names, row)
                }
                for row in cursor.fetchall()
            ]