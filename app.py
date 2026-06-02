from src.timeutils import configure_app_timezone

configure_app_timezone()

from src.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
