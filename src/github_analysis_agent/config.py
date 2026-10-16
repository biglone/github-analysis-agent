"""Load optional project configuration from a local .env file."""

from dotenv import find_dotenv, load_dotenv


def load_project_env():
    """Load the nearest .env found from the current working directory.

    Values already present in the process environment take precedence.
    """
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path=dotenv_path, override=False)
