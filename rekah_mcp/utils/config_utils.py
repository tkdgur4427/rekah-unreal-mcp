"""configuration management utilities"""

import os
import configparser

global_config = configparser.ConfigParser()


def load_config_ini(config_path: str = "./config.ini") -> None:
    """load configuration file

    Args:
        config_path: path to config.ini file
    """
    if os.path.exists(config_path):
        global_config.read(config_path, encoding="utf-8")


def get_config_value(section: str, key: str, default=None):
    """get configuration value

    Args:
        section: config section name
        key: config key name
        default: default value if not found

    Returns:
        config value or default
    """
    try:
        return global_config.get(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError):
        return default


def get_config_int(section: str, key: str, default: int = 0) -> int:
    """get configuration value as integer"""
    value = get_config_value(section, key)
    if value is None:
        return default
    return int(value)


def get_config_bool(section: str, key: str, default: bool = False) -> bool:
    """get configuration value as boolean"""
    value = get_config_value(section, key)
    if value is None:
        return default
    return value.lower() in ("true", "1", "yes", "on")


# auto-load on import
load_config_ini()
