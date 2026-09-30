import hashlib
import json
import time
from pathlib import Path

_ROTATE_BYTES = 10 * 1024 * 1024


def _audit(obj: object, run_dir: Path) -> None:
    """JSONL-аудит с SHA-256 цепочкой (tamper-evident) и ротацией 10МБ."""
    log = run_dir / "audit.log"
    chain = run_dir / "audit.chain"
    try:
        if log.exists() and log.stat().st_size > _ROTATE_BYTES:
            log.rename(run_dir / "audit.log.1")
            chain.unlink(missing_ok=True)
    except OSError:
        pass
    prev = chain.read_text().strip() if chain.exists() else "0" * 64
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": obj, "prev": prev}
    h = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
    entry["hash"] = h
    try:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        chain.write_text(h)
    except OSError:
        pass
