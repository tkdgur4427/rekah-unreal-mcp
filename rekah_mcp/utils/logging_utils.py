"""logging utilities with rich support"""

import os
from datetime import datetime
from rich.console import Console
from rich.theme import Theme
from rekah_mcp.utils.singleton_utils import SingletonInstance


# custom theme for log levels
custom_theme = Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "bold red",
    "debug": "dim white",
})


class Logger(SingletonInstance):
    """singleton logger class with rich support"""

    def __init__(self, prefix: str = "rekah-mcp", log_dir: str = "./logs"):
        """initialize logger

        Args:
            prefix: log message prefix
            log_dir: directory for log files
        """
        self.prefix = prefix
        self.log_dir = log_dir
        self.console = Console(theme=custom_theme)
        self._ensure_log_dir()

    def _ensure_log_dir(self):
        """create log directory if not exists"""
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)

    def _format(self, level: str, message: str) -> str:
        """format log message"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return f"[{timestamp}] [{self.prefix}] {level}: {message}"

    def info(self, message: str):
        """log info level message"""
        self.console.print(self._format("INFO", message), style="info")

    def error(self, message: str):
        """log error level message"""
        self.console.print(self._format("ERROR", message), style="error")

    def warning(self, message: str):
        """log warning level message"""
        self.console.print(self._format("WARNING", message), style="warning")

    def debug(self, message: str):
        """log debug level message"""
        self.console.print(self._format("DEBUG", message), style="debug")


def logging_func(desc: str = ""):
    """decorator for function logging

    Args:
        desc: description of the function
    """
    def decorator(function):
        def wrapper(*args, **kwargs):
            Logger.instance().info(f"[start] {function.__name__} - {desc}")
            result = function(*args, **kwargs)
            Logger.instance().info(f"[end] {function.__name__}")
            return result
        return wrapper
    return decorator
