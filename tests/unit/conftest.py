from unittest.mock import patch


def no_dotenv():
    """Patch context that disables .env file discovery."""
    return patch("seekr_chain.user_config.dotenv.find_dotenv", return_value="")


def no_toml_files():
    """Patch context that disables .seekrchain.toml file discovery (local and global)."""
    return patch("seekr_chain.user_config._find_file_walking_up", return_value=None)
