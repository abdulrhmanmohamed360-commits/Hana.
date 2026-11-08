"""Usage: python -m app.scripts.seed_domains"""
from app.db.session import get_sessionmaker
from app.services.seed import seed_default_domains


def main() -> None:
    with get_sessionmaker()() as db:
        created = seed_default_domains(db)
    print(f"Created {len(created)} domain(s): {', '.join(created) or '-'}")


if __name__ == "__main__":
    main()
