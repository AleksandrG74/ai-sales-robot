import logging
import os
import signal
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOGS_DIR / "orchestrator.log"

HEALTHCHECK_INTERVAL = int(os.getenv("ORCHESTRATOR_HEALTHCHECK_INTERVAL", "30"))
RESTART_DELAY = int(os.getenv("ORCHESTRATOR_RESTART_DELAY", "5"))
MAX_RESTARTS = int(os.getenv("ORCHESTRATOR_MAX_RESTARTS", "0"))


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("orchestrator")
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.propagate = False
    return logger


log = setup_logging()


SERVICES: List[Dict[str, object]] = [
    {
        "name": "dialogues-miner-web",
        "cmd": [
            "python", "-u", "dialogues-miner/main.py",
            "--mode", "web",
            "--port", os.getenv("MINER_WEB_PORT", "8001"),
        ],
        "cwd": str(BASE_DIR),
    },
    {
        "name": "dialogue-worker",
        "cmd": ["python", "-u", "workers/dialogue/main.py"],
        "cwd": str(BASE_DIR),
    },
    {
        "name": "telegram-worker",
        "cmd": ["python", "-u", "workers/telegram/main.py"],
        "cwd": str(BASE_DIR),
    },
    {
        "name": "web-backend",
        "cmd": [
            "uvicorn",
            "web.backend.main:app",
            "--host", "0.0.0.0",
            "--port", os.getenv("WEB_BACKEND_PORT", "8000"),
        ],
        "cwd": str(BASE_DIR),
    },
]


class ServiceProcess:
    def __init__(self, spec: Dict[str, object]) -> None:
        self.name: str = str(spec["name"])
        self.cmd: List[str] = list(spec["cmd"])  # type: ignore[arg-type]
        self.cwd: str = str(spec["cwd"])
        self.proc: Optional[subprocess.Popen] = None
        self.restarts: int = 0
        self.last_start: float = 0.0

    def start(self) -> None:
        try:
            log.info(
                "Starting service '%s': %s (cwd=%s)",
                self.name, " ".join(self.cmd), self.cwd,
            )
            env = os.environ.copy()
            env.setdefault("PYTHONUNBUFFERED", "1")
            self.proc = subprocess.Popen(
                self.cmd,
                cwd=self.cwd,
                env=env,
                stdout=sys.stdout,
                stderr=sys.stderr,
                start_new_session=True,
            )
            self.last_start = time.time()
        except Exception as exc:
            log.exception("Failed to start service '%s': %s", self.name, exc)
            self.proc = None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 10.0) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is not None:
            return
        try:
            log.info("Stopping service '%s' (pid=%s)", self.name, self.proc.pid)
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning(
                    "Service '%s' did not exit in %.1fs, killing",
                    self.name, timeout,
                )
                self.proc.kill()
                try:
                    self.proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    log.error("Service '%s' could not be killed", self.name)
        except Exception as exc:
            log.exception("Error while stopping '%s': %s", self.name, exc)

    def restart(self) -> None:
        self.stop()
        if MAX_RESTARTS > 0 and self.restarts >= MAX_RESTARTS:
            log.error(
                "Service '%s' reached max restarts (%d), not restarting",
                self.name, MAX_RESTARTS,
            )
            return
        time.sleep(RESTART_DELAY)
        self.restarts += 1
        log.warning("Restarting service '%s' (attempt #%d)", self.name, self.restarts)
        self.start()


class Orchestrator:
    def __init__(self, specs: List[Dict[str, object]]) -> None:
        self.services: List[ServiceProcess] = [ServiceProcess(s) for s in specs]
        self._shutdown = False

    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):  # type: ignore[no-untyped-def]
            log.info("Received signal %s, shutting down...", signum)
            self._shutdown = True

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def start_all(self) -> None:
        for svc in self.services:
            svc.start()

    def stop_all(self) -> None:
        for svc in self.services:
            svc.stop()

    def healthcheck(self) -> None:
        for svc in self.services:
            if not svc.is_alive():
                code = svc.proc.returncode if svc.proc is not None else None
                log.error(
                    "Service '%s' is not alive (returncode=%s)", svc.name, code,
                )
                svc.restart()

    def run(self) -> None:
        self._install_signal_handlers()
        log.info("Orchestrator starting, %d services", len(self.services))
        self.start_all()
        try:
            while not self._shutdown:
                for _ in range(HEALTHCHECK_INTERVAL):
                    if self._shutdown:
                        break
                    time.sleep(1)
                if self._shutdown:
                    break
                self.healthcheck()
        except Exception as exc:
            log.exception("Orchestrator main loop error: %s", exc)
        finally:
            log.info("Orchestrator shutting down, stopping services...")
            self.stop_all()
            log.info("Orchestrator stopped")


def main() -> None:
    orch = Orchestrator(SERVICES)
    orch.run()


if __name__ == "__main__":
    main()
