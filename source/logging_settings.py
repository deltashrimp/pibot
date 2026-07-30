import logging
import sys
import traceback
from pathlib import Path

from colorama import Back, Fore, Style

# Кодом великодушно поделился рамзес666

COLORS = {
    "DEBUG": Fore.CYAN,
    "INFO": Fore.GREEN,
    "WARNING": Fore.YELLOW,
    "ERROR": Fore.RED,
    "CRITICAL": Fore.RED + Back.YELLOW,
}


# отдельный класс т. к. собственная логика есть
class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # разделитель
        def sep(char: str) -> str:
            return f"{Fore.LIGHTBLACK_EX}{char}{Style.RESET_ALL}"

        # смена цвета уровней
        color = COLORS[record.levelname]
        levelname = f"{color}{record.levelname + ':':<9}{Style.RESET_ALL}"

        # смена цвета названия модуля
        name = f"{Fore.LIGHTBLACK_EX}{record.name}{Fore.RESET}"

        # получение сообщения
        message = record.getMessage()

        # трейсбек если есть
        exc_text = ""
        if record.exc_info:
            exc_text = traceback.format_exc()
            if exc_text:
                exc_text = f"\n{Fore.RED}{exc_text}{Style.RESET_ALL}"

        return f"{levelname} {name}{sep(':')} {message}{exc_text}"


console_formatter = ConsoleFormatter()


file_formatter = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(filename)s:%(lineno)d | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


# основная функция
def setup_logging(
    console_log_level: int,
    file_log_level: int | None = None,
    file_path: Path | None = None,
) -> None:
    logger = logging.getLogger()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_log_level)
    console_handler.setFormatter(console_formatter)

    logger.addHandler(console_handler)

    logger.setLevel(logging.INFO)

    if file_path is not None:
        file_handler = logging.FileHandler(file_path, encoding="utf-8")
        file_handler.setLevel(file_log_level)
        file_handler.setFormatter(file_formatter)

        logger.addHandler(file_handler)


# фикс логов бекенда, чтобы ошибки 4хх 5хх возвращали не INFO, а WARNING и ERROR
class StatusCodeHandler(logging.StreamHandler):
    def __init__(self, stream: object = None) -> None:
        super().__init__(stream)
        self.setFormatter(console_formatter)

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        last_word = message.split()[-1]

        if not last_word.isdigit() or len(last_word) != 3:
            super().emit(record)
            return None

        status_code = int(last_word)

        if status_code >= 500:
            record.levelno = logging.ERROR
            record.levelname = "ERROR"
        elif status_code >= 400:
            record.levelno = logging.WARNING
            record.levelname = "WARNING"

        # родительский метод для вывода
        super().emit(record)


backend_logger = logging.getLogger("uvicorn.access")
backend_logger.addHandler(StatusCodeHandler(sys.stdout))
