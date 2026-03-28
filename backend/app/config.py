import json
import os
from dotenv import load_dotenv


class Config:
    def __init__(self, config_path="config.json"):
        load_dotenv()
        app_dir = os.path.dirname(os.path.abspath(__file__))
        backend_dir = os.path.dirname(app_dir)
        project_dir = os.path.dirname(backend_dir)

        resolved_config_path = config_path
        if not os.path.isabs(resolved_config_path):
            cwd_candidate = os.path.abspath(os.path.join(os.getcwd(), resolved_config_path))
            project_candidate = os.path.abspath(os.path.join(project_dir, resolved_config_path))
            backend_candidate = os.path.abspath(os.path.join(backend_dir, resolved_config_path))
            if os.path.exists(cwd_candidate):
                resolved_config_path = cwd_candidate
            elif os.path.exists(project_candidate):
                resolved_config_path = project_candidate
            else:
                resolved_config_path = backend_candidate

        self.config_path = os.path.abspath(resolved_config_path)
        self.BASE_DIR = os.path.dirname(os.path.dirname(self.config_path))

        with open(self.config_path, encoding="utf-8") as f:
            self._data = json.load(f)

    def get(self, key, default=None):
        env_key = key.replace('.', '_').upper()
        return os.getenv(env_key) or self._data.get(key, default)

    def get_path(self, key, default=None):
        value = self.get(key, default)
        if isinstance(value, str) and not os.path.isabs(value):
            return os.path.join(self.BASE_DIR, value)
        return value
