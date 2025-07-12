import logging
from PyQt6.QtCore import QObject, pyqtSignal

class QtLogHandler(QObject, logging.Handler):
    """
    A custom logging handler that emits a signal for each log record.
    Connect this signal to a slot in your GUI to display logs.
    """
    log_received = pyqtSignal(str, str)  # Signal emitting log level and message

    def __init__(self, *args, **kwargs):
        QObject.__init__(self, *args, **kwargs)
        logging.Handler.__init__(self)

    def emit(self, record):
        """
        Emits a signal with the formatted log message.
        """
        msg = self.format(record)
        self.log_received.emit(record.levelname, msg)