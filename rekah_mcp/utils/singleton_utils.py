"""singleton pattern base class implementation"""


class SingletonInstance:
    """base class for singleton pattern implementation"""

    __instance = None

    @classmethod
    def instance(cls, *args, **kwargs):
        """create or get the singleton instance"""
        if cls.__instance is None:
            cls.__instance = cls(*args, **kwargs)
        return cls.__instance

    @classmethod
    def reset_instance(cls):
        """reset the singleton instance (for testing)"""
        cls.__instance = None
