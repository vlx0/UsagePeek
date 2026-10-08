"""Read local on-disk usage. Never copies huge DBs. No network."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .settings import CustomSource
from .settings import load as load_custom

MAX_JSONL_FILES = 500
MAX_JSONL_BYTES = 12_000_000
MODEL_SUFFIXES = {".gguf", ".safetensors", ".bin", ".onnx", ".mlmodel"}


@dataclass
class SourceStat:
    id: str
    label: str
    found: bool
    path: str = ""
    kind: str = "agent"  # "agent" | "local"
    sessions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    cost_usd: float = 0.0
    extra: str = ""
    models: list[tuple[str, int]] = field(default_factory=list)
    model_files: int = 0
    model_bytes: int = 0
    running: bool | None = None
    removable: bool = False
    note: str = ""
    error: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_tokens


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or (_home() / "AppData" / "Roaming"))


def since_ms(period: str) -> int | None:
    now = datetime.now()
    if period == "all":
        return None
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "7d":
        from datetime import timedelta

        start = start - timedelta(days=6)
    return int(start.timestamp() * 1000)


def _open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)


def _as_int(x) -> int:
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    """Localhost-only probe: is the runner serving right now?"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.15)
        return sock.connect_ex((host, port)) == 0


def _model_files(root: Path, limit: int = 4000) -> tuple[int, int]:
    count = total = 0
    if not root.exists():
        return 0, 0
    for path in root.rglob("*"):
        if count >= limit:
            break
        if not path.is_file() or path.suffix.lower() not in MODEL_SUFFIXES:
            continue
        try:
            total += path.stat().st_size
        except OSError:
            continue
        count += 1
    return count, total


def _sqlite_token_sums(root: Path, since: int | None) -> tuple[int, int]:
    """Any *.db / *.sqlite under root with input/output token columns."""
    inn = out = 0
    if not root.exists():
        return 0, 0
    patterns = ("*.db", "*.sqlite", "*.sqlite3")
    files: list[Path] = []
    for pattern in patterns:
        files.extend(root.rglob(pattern))
    for db in files[:40]:
        try:
            con = _open_ro(db)
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
                lower = {c.lower() for c in cols}
                if not (lower & {"input_tokens", "prompt_tokens"}):
                    continue
                in_c = next(c for c in cols if c.lower() in ("input_tokens", "prompt_tokens"))
                out_c = next(
                    (c for c in cols if c.lower() in ("output_tokens", "completion_tokens")), None
                )
                if out_c is None:
                    continue
                ts_c = next(
                    (c for c in cols if c.lower() in ("ts", "timestamp", "created_at", "created_at_ms", "time")),
                    None,
                )
                query = f'SELECT COALESCE(SUM("{in_c}"),0), COALESCE(SUM("{out_c}"),0) FROM "{table}"'
                args: tuple = ()
                if ts_c and since:
                    query += f' WHERE "{ts_c}" >= ?'
                    args = (since,)
                row = con.execute(query, args).fetchone()
                inn += _as_int(row[0])
                out += _as_int(row[1])
            con.close()
        except sqlite3.Error:
            continue
    return inn, out


def _usage_from_mapping(obj: dict) -> tuple[int, int, int]:
    inn = _as_int(
        obj.get("input_tokens")
        or obj.get("prompt_tokens")
        or obj.get("inputTokens")
    )
    out = _as_int(
        obj.get("output_tokens")
        or obj.get("completion_tokens")
        or obj.get("outputTokens")
    )
    cache = _as_int(
        obj.get("cache_read_input_tokens")
        or obj.get("cache_read_tokens")
        or obj.get("cacheReadTokens")
        or obj.get("cache_creation_input_tokens")
        or obj.get("cache_write_tokens")
    )
    return inn, out, cache


def _walk_collect_usage(obj, acc: list) -> None:
    if isinstance(obj, dict):
        keys = set(obj)
        if keys & {
            "input_tokens",
            "output_tokens",
            "prompt_tokens",
            "completion_tokens",
            "inputTokens",
            "outputTokens",
        }:
            acc.append(obj)
        for v in obj.values():
            _walk_collect_usage(v, acc)
    elif isinstance(obj, list):
        for v in obj[:40]:
            _walk_collect_usage(v, acc)


def _scan_jsonl_tree(root: Path, since: int | None) -> tuple[int, int, int, int, dict[str, int]]:
    inn = out = cache = files = 0
    models: dict[str, int] = defaultdict(int)
    if not root.exists():
        return 0, 0, 0, 0, {}
    n = 0
    for path in root.rglob("*"):
        if n >= MAX_JSONL_FILES:
            break
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".jsonl", ".json"}:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_size > MAX_JSONL_BYTES:
            continue
        mtime_ms = int(st.st_mtime * 1000)
        if since and mtime_ms < since and path.suffix.lower() == ".json":
            continue
        n += 1
        files += 1
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = raw.splitlines() if path.suffix.lower() == ".jsonl" else [raw]
        for line in lines:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = obj.get("timestamp") or obj.get("ts") or obj.get("created_at")
            if since and isinstance(ts, (int, float)):
                tms = int(ts if ts > 1e12 else ts * 1000)
                if tms < since:
                    continue
            found: list[dict] = []
            _walk_collect_usage(obj, found)
            model = ""
            if isinstance(obj, dict):
                msg = obj.get("message")
                if isinstance(msg, dict):
                    model = str(msg.get("model") or "")
                model = model or str(obj.get("model") or "")
            for u in found:
                i, o, c = _usage_from_mapping(u)
                inn += i
                out += o
                cache += c
                if model:
                    models[model] += i + o
    return inn, out, cache, files, dict(models)


def scan_cursor(since: int | None) -> SourceStat:
    db = _appdata() / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    stat = SourceStat(
        id="cursor",
        label="Cursor",
        found=db.exists(),
        path=str(db.parent if db.exists() else _appdata() / "Cursor"),
        note="В state.vscdb токены запросов не пишутся (обычно нули). Ниже — сессии и % контекста.",
    )
    if not db.exists():
        return stat
    try:
        con = _open_ro(db)
        rows = con.execute(
            "SELECT composerId, lastUpdatedAt, value FROM composerHeaders"
        ).fetchall()
        con.close()
    except sqlite3.Error as e:
        stat.error = str(e)
        return stat

    n = lines_add = lines_rm = files = 0
    pcts: list[float] = []
    for _cid, updated, raw in rows:
        if since and _as_int(updated) < since:
            continue
        n += 1
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        p = obj.get("contextUsagePercent")
        if isinstance(p, (int, float)) and p > 0:
            pcts.append(float(p))
        lines_add += _as_int(obj.get("totalLinesAdded"))
        lines_rm += _as_int(obj.get("totalLinesRemoved"))
        files += _as_int(obj.get("filesChangedCount"))
    stat.sessions = n
    bits = []
    if pcts:
        bits.append(f"контекст ~{sum(pcts) / len(pcts):.0f}%")
    if files:
        bits.append(f"файлов {files}")
    if lines_add or lines_rm:
        bits.append(f"+{lines_add}/−{lines_rm} строк")
    stat.extra = " · ".join(bits)
    return stat


def scan_minimax(since: int | None) -> SourceStat:
    root = Path(os.environ.get("MINIMAX_DATA_DIR") or os.environ.get("MINIMAX_HOME") or (_home() / ".minimax"))
    db = root / "v2" / "sqlite" / "runtime-state.sqlite"
    legacy = root / "sqlite.db"
    stat = SourceStat(id="minimax", label="MiniMax Code", found=root.exists(), path=str(root))
    if not root.exists():
        return stat
    target = db if db.exists() else legacy
    if not target.exists():
        stat.note = "Папка есть, таблицы usage не найдены."
        return stat
    try:
        con = _open_ro(target)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        q_since = since or 0
        if "local_runtime_token_usage" in tables:
            row = con.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(input_tokens),0),
                       COALESCE(SUM(output_tokens),0),
                       COALESCE(SUM(cache_read_tokens)+SUM(cache_write_tokens),0),
                       COALESCE(SUM(cost_usd),0),
                       COUNT(DISTINCT session_id)
                FROM local_runtime_token_usage
                WHERE ts >= ?
                """,
                (q_since,),
            ).fetchone()
            stat.sessions = _as_int(row[5])
            stat.input_tokens = _as_int(row[1])
            stat.output_tokens = _as_int(row[2])
            stat.cache_tokens = _as_int(row[3])
            stat.cost_usd = _as_float(row[4])
            models = con.execute(
                """
                SELECT COALESCE(NULLIF(model,''), '—') AS m,
                       SUM(input_tokens+output_tokens)
                FROM local_runtime_token_usage
                WHERE ts >= ?
                GROUP BY m
                ORDER BY 2 DESC
                LIMIT 8
                """,
                (q_since,),
            ).fetchall()
            stat.models = [(str(m), _as_int(t)) for m, t in models]
        elif "token_usage" in tables:
            row = con.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(input_tokens),0),
                       COALESCE(SUM(output_tokens),0),
                       COALESCE(SUM(cost_usd),0)
                FROM token_usage
                WHERE ts >= ?
                """,
                (q_since,),
            ).fetchone()
            stat.sessions = _as_int(row[0])
            stat.input_tokens = _as_int(row[1])
            stat.output_tokens = _as_int(row[2])
            stat.cost_usd = _as_float(row[3])
        con.close()
    except sqlite3.Error as e:
        stat.error = str(e)
    return stat


def scan_claude(since: int | None) -> SourceStat:
    root = _home() / ".claude"
    projects = root / "projects"
    transcripts = root / "transcripts"
    stat = SourceStat(id="claude", label="Claude Code", found=root.exists(), path=str(root))
    if not root.exists():
        return stat
    inn = out = cache = files = 0
    models: dict[str, int] = {}
    for folder in (projects, transcripts):
        i, o, c, f, m = _scan_jsonl_tree(folder, since)
        inn += i
        out += o
        cache += c
        files += f
        for k, v in m.items():
            models[k] = models.get(k, 0) + v
    stat.input_tokens = inn
    stat.output_tokens = out
    stat.cache_tokens = cache
    stat.sessions = files
    stat.models = sorted(models.items(), key=lambda x: -x[1])[:8]
    if files == 0:
        stat.note = "Папка есть, jsonl с usage не найдены."
    return stat


def scan_codex(since: int | None) -> SourceStat:
    root = Path(os.environ.get("CODEX_HOME") or (_home() / ".codex"))
    stat = SourceStat(id="codex", label="Codex", found=root.exists(), path=str(root))
    if not root.exists():
        return stat
    inn = out = cache = files = 0
    models: dict[str, int] = {}
    for name in ("sessions", "archived_sessions"):
        i, o, c, f, m = _scan_jsonl_tree(root / name, since)
        inn += i
        out += o
        cache += c
        files += f
        for k, v in m.items():
            models[k] = models.get(k, 0) + v
    stat.input_tokens = inn
    stat.output_tokens = out
    stat.cache_tokens = cache
    stat.sessions = files
    stat.models = sorted(models.items(), key=lambda x: -x[1])[:8]
    if files == 0:
        stat.note = "Папка есть, сессии не найдены."
    return stat


def scan_opencode(since: int | None) -> SourceStat:
    xdg = Path(os.environ.get("XDG_DATA_HOME") or (_home() / ".local" / "share"))
    root = xdg / "opencode"
    stat = SourceStat(id="opencode", label="OpenCode", found=root.exists(), path=str(root))
    if not root.exists():
        return stat
    inn = out = cache = files = 0
    models: dict[str, int] = {}
    for db in root.glob("*.db"):
        try:
            con = _open_ro(db)
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in names:
                cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
                colset = {c.lower() for c in cols}
                if not ({"input_tokens", "output_tokens"} & colset):
                    continue
                in_c = next(c for c in cols if c.lower() in ("input_tokens", "prompt_tokens"))
                out_c = next(c for c in cols if c.lower() in ("output_tokens", "completion_tokens"))
                ts_c = next((c for c in cols if c.lower() in ("ts", "timestamp", "created_at", "time")), None)
                q = f"SELECT COALESCE(SUM({in_c}),0), COALESCE(SUM({out_c}),0) FROM {table}"
                args: tuple = ()
                if ts_c and since:
                    q += f" WHERE {ts_c} >= ?"
                    args = (since,)
                row = con.execute(q, args).fetchone()
                inn += _as_int(row[0])
                out += _as_int(row[1])
            con.close()
        except sqlite3.Error:
            continue
    i, o, c, f, m = _scan_jsonl_tree(root / "storage", since)
    inn += i
    out += o
    cache += c
    files += f
    models.update(m)
    stat.input_tokens = inn
    stat.output_tokens = out
    stat.cache_tokens = cache
    stat.sessions = files
    stat.models = sorted(models.items(), key=lambda x: -x[1])[:8]
    return stat


def scan_copilot(_since: int | None) -> SourceStat:
    root = _home() / ".copilot"
    stat = SourceStat(id="copilot", label="GitHub Copilot", found=root.exists(), path=str(root))
    if not root.exists():
        return stat
    db = root / "data.db"
    if db.exists():
        stat.note = "Есть data.db — токены Copilot в этой версии не разбираем (схема закрытая)."
    else:
        stat.note = "Локальных счётчиков токенов нет, только служебные логи."
    return stat


def scan_ollama(since: int | None) -> SourceStat:
    models_root = Path(os.environ.get("OLLAMA_MODELS") or (_home() / ".ollama" / "models"))
    root = models_root.parent
    app = Path(os.environ.get("LOCALAPPDATA") or (_home() / "AppData" / "Local")) / "Ollama"
    stat = SourceStat(
        id="ollama",
        label="Ollama",
        found=root.exists() or app.exists(),
        path=str(root if root.exists() else app),
        kind="local",
    )
    if not stat.found:
        return stat

    names: list[tuple[str, int]] = []
    manifests = models_root / "manifests"
    if manifests.exists():
        for path in manifests.rglob("*"):
            if path.is_file():
                parts = path.relative_to(manifests).parts
                tag = f"{parts[-2]}:{parts[-1]}" if len(parts) >= 2 else path.name
                names.append((tag, 0))
    blobs_files, blobs_bytes = _model_files(models_root / "blobs")
    if blobs_bytes == 0 and (models_root / "blobs").exists():
        # Ollama blobs have no extension — size the directory directly.
        blobs_files = 0
        for path in (models_root / "blobs").glob("*"):
            if path.is_file():
                try:
                    blobs_bytes += path.stat().st_size
                except OSError:
                    continue
                blobs_files += 1
    stat.model_files = len(names) or blobs_files
    stat.model_bytes = blobs_bytes
    stat.models = names[:8]
    stat.running = _port_open(11434)

    db = app / "db.sqlite"
    if db.exists():
        try:
            con = _open_ro(db)
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "chats" in tables:
                stat.sessions = _as_int(con.execute("SELECT COUNT(*) FROM chats").fetchone()[0])
            if "messages" in tables:
                msgs = _as_int(con.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
                if msgs:
                    stat.extra = f"сообщений {msgs}"
            con.close()
        except sqlite3.Error as e:
            stat.error = str(e)
    stat.note = "Ollama не хранит счётчик токенов — показываем модели и состояние сервера."
    return stat


def scan_lmstudio(since: int | None) -> SourceStat:
    root = _home() / ".lmstudio"
    alt = _home() / ".cache" / "lm-studio"
    if not root.exists() and alt.exists():
        root = alt
    stat = SourceStat(
        id="lmstudio",
        label="LM Studio",
        found=root.exists(),
        path=str(root),
        kind="local",
    )
    if not root.exists():
        return stat

    stat.model_files, stat.model_bytes = _model_files(root / "models")
    stat.running = _port_open(1234)

    convs = root / "conversations"
    inn = out = files = 0
    if convs.exists():
        i, o, _c, files, _m = _scan_jsonl_tree(convs, since)
        inn += i
        out += o
    stat.sessions = files
    stat.input_tokens = inn
    stat.output_tokens = out
    if inn == 0 and out == 0:
        stat.note = "Счётчиков токенов в файлах нет — показываем модели и состояние сервера."
    return stat


def scan_custom(source: CustomSource, since: int | None) -> SourceStat:
    root = Path(source.path)
    stat = SourceStat(
        id=source.id,
        label=source.label,
        found=root.exists(),
        path=source.path,
        kind="local",
        removable=True,
    )
    if not root.exists():
        stat.note = "Папка не найдена."
        return stat

    stat.model_files, stat.model_bytes = _model_files(root)
    db_in, db_out = _sqlite_token_sums(root, since)
    j_in, j_out, cache, files, models = _scan_jsonl_tree(root, since)
    stat.input_tokens = db_in + j_in
    stat.output_tokens = db_out + j_out
    stat.cache_tokens = cache
    stat.sessions = files
    stat.models = sorted(models.items(), key=lambda x: -x[1])[:8]
    if stat.total_tokens == 0 and stat.model_files == 0:
        stat.note = "Ни счётчиков токенов, ни файлов моделей не нашли."
    return stat


def collect(period: str) -> list[SourceStat]:
    since = since_ms(period)
    rows = [
        scan_cursor(since),
        scan_minimax(since),
        scan_claude(since),
        scan_codex(since),
        scan_opencode(since),
        scan_copilot(since),
        scan_ollama(since),
        scan_lmstudio(since),
    ]
    for source in load_custom():
        rows.append(scan_custom(source, since))
    return rows
