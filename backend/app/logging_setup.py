import logging
import os
import threading


class LineRotatingFileHandler(logging.Handler):
    """Rotate log files by line count using a fixed-size circular file set."""

    def __init__(self, log_dir, base_name="app", max_lines=100000, max_files=5, encoding="utf-8"):
        super().__init__()
        self.log_dir = log_dir
        self.base_name = base_name
        self.max_lines = max(1, int(max_lines))
        self.max_files = max(1, int(max_files))
        self.encoding = encoding
        self._lock = threading.RLock()
        self._file = None
        self._current_index = 0
        self._current_lines = 0
        self._closed = False

        os.makedirs(self.log_dir, exist_ok=True)
        self._initialize_target_file()

    def _path_for(self, index):
        return os.path.join(self.log_dir, f"{self.base_name}.{index}.log")

    def _count_lines(self, path):
        if not os.path.exists(path):
            return 0
        with open(path, "r", encoding=self.encoding, errors="ignore") as f:
            return sum(1 for _ in f)

    def _initialize_target_file(self):
        max_seen = -1
        selected_index = 0
        for idx in range(self.max_files):
            path = self._path_for(idx)
            if os.path.exists(path):
                mtime = os.path.getmtime(path)
                if mtime > max_seen:
                    max_seen = mtime
                    selected_index = idx

        self._current_index = selected_index
        self._open_current_file(append=True)
        self._current_lines = self._count_lines(self._path_for(self._current_index))
        if self._current_lines >= self.max_lines:
            self._rotate()

    def _open_current_file(self, append=True):
        if self._file:
            try:
                self._file.close()
            except Exception:
                pass
        mode = "a" if append else "w"
        self._file = open(self._path_for(self._current_index), mode, encoding=self.encoding)

    def _rotate(self):
        self._current_index = (self._current_index + 1) % self.max_files
        self._open_current_file(append=False)
        self._current_lines = 0

    def emit(self, record):
        try:
            msg = self.format(record)
            with self._lock:
                # During interpreter/app shutdown, logging may still emit after close().
                if self._closed:
                    return

                # Defensive reopen to avoid rare None races or external file closure.
                if self._file is None:
                    self._open_current_file(append=True)

                if self._current_lines >= self.max_lines:
                    self._rotate()
                self._file.write(msg + "\n")
                self._file.flush()
                self._current_lines += 1
        except Exception:
            self.handleError(record)

    def close(self):
        with self._lock:
            self._closed = True
            if self._file:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None
        super().close()


def setup_logging(log_dir="backend/logs", max_lines=100000, max_files=5, level="INFO"):
    root = logging.getLogger()
    if getattr(root, "_line_logging_initialized", False):
        return root

    log_level = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(log_level)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(threadName)s | %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)

    root.handlers.clear()
    root.addHandler(console_handler)

    try:
        file_handler = LineRotatingFileHandler(
            log_dir=log_dir,
            base_name="app",
            max_lines=max_lines,
            max_files=max_files,
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except Exception as e:
        root.error("File logging disabled, cannot initialize log dir '%s': %s", log_dir, e, exc_info=True)

    root._line_logging_initialized = True
    return root
