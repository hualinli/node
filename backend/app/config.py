import json
import os
from dotenv import load_dotenv

class Config:
    def __init__(self, config_path="config.json"):
        load_dotenv()
        self.BASE_DIR = os.getcwd()
        if not os.path.isabs(config_path):
            config_path = os.path.join(self.BASE_DIR, config_path)
        with open(config_path) as f:
            self._data = json.load(f)

    def get(self, key, default=None):
        env_key = key.replace('.', '_').upper()
        return os.getenv(env_key) or self._data.get(key, default)

    def get_path(self, key, default=None):
        value = self.get(key, default)
        if isinstance(value, str) and not os.path.isabs(value):
            return os.path.join(self.BASE_DIR, value)
        return value
