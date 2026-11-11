from app.database import create_schema, seed_data
from app.mcp_server import contextcore_mcp


def main() -> None:
    create_schema()
    seed_data()
    contextcore_mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
