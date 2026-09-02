"""Cockpit PTY bridge — the agent can hand a browser a terminal (CFOP-75).

Phase A of the CFOP-59 capstone, server half. Everything the cockpit already
does puts a briefed session *somewhere*; this is the piece that lets a browser
reach one, for the host tiers, where the agent already holds the ssh key.

Three decisions are load-bearing, and each is the answer to a question that
looked cheaper than it was.

**Its own listener, not the console's.** ``web_server.py`` runs under Waitress,
which cannot upgrade a connection — ``WEBSOCKET_AVAILABLE`` has been ``False``
since it was written. Nor can the server simply be swapped: the console is a
thread inside the agent process (``agent.py`` calls ``run_threaded()``), and
gunicorn or uvicorn want to own the process. So this is a small asyncio
listener on its own port, in its own thread, and nothing about the console
changes. It also keeps terminals off Waitress's eight threads, which a session
would otherwise hold for hours.

**argv in, bytes out.** The bridge does not know what a tier is. The ladder
already returns ``attach_argv`` — the exact argv an operator would run — so
this runs *that* under a PTY and pumps bytes. Placement, dedupe, TTL and
cleanup stay where they already live, and the bridge gains nothing it could
misuse: it can only run the argv the ladder produced, for a session that
already exists. It cannot spawn one; that stays the admin-gated console route.

**Everything that can refuse, refuses in one pure function.** ``authorize()``
takes strings and returns a verdict. No socket, no subprocess, no asyncio — so
the interesting half (a foreign origin, a read-scoped token, a tier this phase
does not serve) is unit-testable without a network, and a contributor who adds
a fifth way in has to add it there.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import signal
import struct
import termios
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Sequence, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger("cfoperator.cockpit.bridge")

#: Its own port rather than the console's, for the reason in the module
#: docstring. 8084 simply because 8083 is the console.
DEFAULT_BRIDGE_PORT = 8084

#: The scope a session needs. Matches what `cfassist attach` mints for an
#: interactive session, so a browser cockpit is not a higher privilege than
#: the terminal one — the same person, the same reach.
REQUIRED_SCOPE = "investigate"

#: Tier 1 is Phase B. Its attach argv is `kubectl attach`, and no service
#: identity in this system holds pods/attach — deliberately, and that grant is
#: CFOP-59's decision to make explicitly. Refused by name here so the failure
#: reads as "not yet" rather than as an unreadable kubectl permissions error.
TIER_POD = "pod"

# Close codes. Distinct per reason because the console has to be able to say
# *which* wall it hit: "sign in again" and "ask an admin for investigate scope"
# and "this one is in the cluster" are three different next actions, and a
# single 1008 would make them one shrug.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NO_SESSION = 4404
CLOSE_TIER_UNSUPPORTED = 4409
#: The cap below. 4429 mirrors HTTP 429 so the number itself is a hint.
CLOSE_BUSY = 4429

#: How long start() waits for the listener thread to bind or fail.
BIND_TIMEOUT_SECONDS = 5

#: How long a client gets to send its auth frame before the socket is dropped.
#: An unauthenticated connection holding a slot is the cheapest possible
#: nuisance, so it does not get to hold one for long.
AUTH_TIMEOUT_SECONDS = 10


@dataclass
class BridgeConfig:
    """Everything the listener needs, resolved once at startup."""

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = DEFAULT_BRIDGE_PORT
    #: Origins allowed to open a terminal. Empty means "refuse every browser":
    #: a PTY that any page can open is the sharp object this design exists to
    #: keep blunt, so the safe default is closed rather than open.
    allowed_origins: Tuple[str, ...] = field(default_factory=tuple)
    #: How many terminals may be open at once. Each is an ssh child on the
    #: agent, and an `investigate` token would otherwise be a licence to fork
    #: as many as the box will take. Two, matching the cockpit concurrency cap
    #: the ladder already enforces per host — a browser should not be able to
    #: exceed what `--spawn` allows.
    max_sessions: int = 2
    #: Serve tier `pod` too (CFOP-59 Phase B). Off by default and independent of
    #: the chart's `pods/attach` grant: turning this on without the grant lets
    #: the attach reach a kubectl that the cluster then refuses, which is a
    #: readable failure; turning on the grant without this keeps the bridge
    #: refusing by name. Both together is the deliberate act, made once.
    pod_tier: bool = False


def build_bridge_config(agent_config: Any) -> BridgeConfig:
    """Bridge config from the ``cockpit:`` block, env, and defaults.

    Same block and the same env-wins precedence as the ladder's config, so
    there is one place an operator looks for cockpit settings rather than two.
    """
    cfg = agent_config if isinstance(agent_config, dict) else {}
    block = cfg.get("cockpit") if isinstance(cfg.get("cockpit"), dict) else {}

    def _str(env: str, key: str, default: str) -> str:
        return str(os.getenv(env) or block.get(key) or default).strip()

    def _int(env: str, key: str, default: int) -> int:
        try:
            return int(os.getenv(env) or block.get(key) or default)
        except (TypeError, ValueError):
            return default

    def _bool(env: str, key: str, default: bool) -> bool:
        raw = os.getenv(env)
        if raw is None:
            raw = block.get(key, default)
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    raw_origins = os.getenv("CFOP_COCKPIT_BRIDGE_ORIGINS")
    if raw_origins is None:
        raw_origins = block.get("bridge_origins") or ""
    if isinstance(raw_origins, (list, tuple)):
        origins = [str(o).strip() for o in raw_origins]
    else:
        origins = [o.strip() for o in str(raw_origins).split(",")]

    return BridgeConfig(
        enabled=_bool("CFOP_COCKPIT_BRIDGE_ENABLED", "bridge_enabled", False),
        host=_str("CFOP_COCKPIT_BRIDGE_BIND", "bridge_bind", "0.0.0.0"),
        port=_int("CFOP_COCKPIT_BRIDGE_PORT", "bridge_port", DEFAULT_BRIDGE_PORT),
        max_sessions=max(1, _int("CFOP_COCKPIT_BRIDGE_MAX_SESSIONS",
                                 "bridge_max_sessions", 2)),
        pod_tier=_bool("CFOP_COCKPIT_BRIDGE_POD_TIER", "bridge_pod_tier", False),
        allowed_origins=tuple(normalize_origin(o) for o in origins if o.strip()),
    )


def normalize_origin(origin: str) -> str:
    """Scheme + host + port, lowercased, no path or trailing slash.

    Browsers send exactly this, but humans configuring `bridge_origins` write
    whatever they had in the address bar. Comparing raw strings would reject a
    correctly-configured console over a trailing slash, mid-incident, with a
    message about origins.
    """
    text = str(origin or "").strip()
    if not text:
        return ""
    parts = urlsplit(text if "//" in text else "//" + text)
    scheme = (parts.scheme or "http").lower()
    netloc = (parts.netloc or "").lower()
    return f"{scheme}://{netloc}" if netloc else ""


def scrub(text: str, limit: int = 160) -> str:
    """Make a message safe to put in front of a terminal.

    Reasons are rendered by a console that may be an xterm, and some of the
    text in them originates on the far host (ssh stderr, a login banner). An
    escape sequence arriving through an error message is a small thing that
    would be very annoying to discover later.
    """
    cleaned = "".join(ch for ch in str(text or "") if ch.isprintable())
    return cleaned[:limit]


@dataclass
class Verdict:
    """The answer to "may this connection have a terminal, and to what"."""

    ok: bool
    #: Session coordinates from the ladder, present only when ok.
    session: Optional[Dict[str, Any]] = None
    #: Machine-readable failure, for the console to key off.
    code: int = 0
    reason: str = ""
    #: Who it turned out to be — for the audit line, present when ok.
    actor: str = ""


def parse_path(path: str) -> Optional[int]:
    """``/cockpit/1889`` -> ``1889``; anything else -> None.

    Strict rather than forgiving: the id names a session on a machine, so a
    path this does not recognise is a request to be refused, not guessed at.
    """
    text = str(path or "").split("?", 1)[0].rstrip("/")
    prefix = "/cockpit/"
    if not text.startswith(prefix):
        return None
    tail = text[len(prefix):]
    if not tail.isdigit():
        return None
    value = int(tail)
    return value if value > 0 else None


def authorize(*, path: str, origin: str, token: str,
              config: BridgeConfig,
              token_verifier: Callable[[str], Any],
              resolver: Callable[[int], Optional[Dict[str, Any]]]) -> Verdict:
    """Decide, without touching a socket or a process.

    Order matters, and it is cheapest-first *except* that the origin check
    comes before the token is even looked at: a page that should not be able
    to reach this must not get to make the agent do database work, and must
    not learn whether a token was good.
    """
    investigation_id = parse_path(path)
    if investigation_id is None:
        return Verdict(False, code=CLOSE_NO_SESSION,
                       reason="not a cockpit path")

    normalized = normalize_origin(origin)
    if not config.allowed_origins:
        return Verdict(False, code=CLOSE_FORBIDDEN,
                       reason="no console origin is configured for the bridge")
    if normalized not in config.allowed_origins:
        # The offered origin is deliberately not echoed back: it is attacker
        # -controlled text and this string reaches a terminal.
        return Verdict(False, code=CLOSE_FORBIDDEN, reason="origin not allowed")

    if not token:
        return Verdict(False, code=CLOSE_UNAUTHENTICATED, reason="no token")
    try:
        identity = token_verifier(token)
    except Exception as e:  # a broken auth store must not read as a bad token
        logger.error("bridge: token verification failed: %s", e, exc_info=True)
        return Verdict(False, code=CLOSE_UNAUTHENTICATED,
                       reason="token could not be verified")
    if identity is None:
        return Verdict(False, code=CLOSE_UNAUTHENTICATED, reason="invalid token")
    if not _has_scope(identity, REQUIRED_SCOPE):
        return Verdict(False, code=CLOSE_FORBIDDEN,
                       reason=f"token lacks the {REQUIRED_SCOPE} scope")

    try:
        session = resolver(investigation_id)
    except Exception as e:
        # Surfaced rather than flattened: "the host could not be probed" is a
        # different problem from "there is no session", and during an incident
        # it is frequently *the* problem.
        logger.error("bridge: session lookup for #%s failed: %s",
                     investigation_id, e, exc_info=True)
        return Verdict(False, code=CLOSE_NO_SESSION,
                       reason=f"session lookup failed: {scrub(e)}")
    if not session:
        # Deliberately not "spawn one": spawning is a workload and a minted
        # credential, and it stays the admin-gated console route.
        return Verdict(False, code=CLOSE_NO_SESSION,
                       reason=f"no live cockpit for investigation {investigation_id}")

    tier = str(session.get("tier") or "")
    if tier == TIER_POD and not config.pod_tier:
        return Verdict(False, code=CLOSE_TIER_UNSUPPORTED,
                       reason="this cockpit is a pod; the browser bridge serves "
                              "the host tiers only unless cockpit.bridge_pod_tier "
                              "is on — use cfassist attach --spawn")

    argv = session.get("attach_argv")
    if not isinstance(argv, (list, tuple)) or not argv:
        return Verdict(False, code=CLOSE_NO_SESSION,
                       reason="session has no attach coordinates")

    # The argv is copied too: a shallow dict() leaves the ladder holding the
    # same list this is about to hand to a subprocess.
    copied = dict(session)
    copied["attach_argv"] = list(argv)
    return Verdict(True, session=copied, actor=_actor_name(identity))


def _has_scope(identity: Any, scope: str) -> bool:
    """Scope check against the auth store's identity, or a plain dict.

    ``has_scope`` first because that is what ``TokenIdentity`` actually
    defines; ``has`` is accepted after it only so a caller injecting a
    different identity type is not forced to match our spelling. Falling
    through to ``.scopes`` is correct either way — the store expands implied
    scopes at verify time, so `remediate` already contains `investigate`.
    """
    for name in ("has_scope", "has"):
        method = getattr(identity, name, None)
        if callable(method):
            return bool(method(scope))
    scopes = getattr(identity, "scopes", None)
    if scopes is None and isinstance(identity, dict):
        scopes = identity.get("scopes")
    return scope in (scopes or ())


def _actor_name(identity: Any) -> str:
    for attr in ("username", "label", "token_prefix"):
        value = getattr(identity, attr, None)
        if value:
            return str(value)
    if isinstance(identity, dict):
        for key in ("username", "label", "token_prefix"):
            if identity.get(key):
                return str(identity[key])
    return "unknown"


async def write_all(loop, fd: int, data: bytes) -> None:  # pragma: no cover
    """Write every byte to a non-blocking fd.

    The pty is non-blocking so a slow reader never stalls the event loop — but
    that makes ``os.write`` free to write *part* of the buffer, or none of it
    and raise ``BlockingIOError``. A bare write therefore drops input, and the
    case that hits it is a large paste, which is precisely what someone does
    with a command they were given mid-incident.
    """
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            waiter = loop.create_future()

            def _writable():
                if not waiter.done():
                    waiter.set_result(None)

            loop.add_writer(fd, _writable)
            try:
                await waiter
            finally:
                loop.remove_writer(fd)
            continue
        except (BrokenPipeError, OSError):
            return  # the far side is gone; the read side will end the session
        view = view[written:]


class PtySession:
    """A child process on the far end of a pseudo-terminal.

    ``pty.fork()`` rather than Popen with a slave fd: the child needs the tty
    as its *controlling* terminal, or the remote shell gets no job control and
    ssh declines to allocate a remote pty. Popen would need a preexec dance to
    reach the same place.
    """

    def __init__(self, argv: Sequence[str]):
        self.argv = list(argv)
        self.pid = -1
        self.fd = -1

    @staticmethod
    def _reap(pid: int, deadline_seconds: float) -> bool:
        """Poll waitpid until the child is gone or the deadline passes."""
        end = time.monotonic() + deadline_seconds
        while True:
            try:
                done, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return True  # already reaped
            if done:
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.05)

    def start(self) -> None:
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - the child never returns
            try:
                os.execvp(self.argv[0], self.argv)
            except Exception:
                os._exit(126)
        self.pid, self.fd = pid, fd
        os.set_blocking(fd, False)

    def resize(self, cols: int, rows: int) -> None:
        """Tell the far side the window changed.

        Clamped rather than trusted: these arrive from a browser, and the
        ioctl takes unsigned shorts.
        """
        if self.fd < 0:
            return
        try:
            cols = max(1, min(int(cols), 1000))
            rows = max(1, min(int(rows), 1000))
        except (TypeError, ValueError):
            # `{"type":"resize","cols":null}` is a browser bug, not a reason to
            # drop someone's session mid-incident.
            return
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            return

    def close(self, *, grace_seconds: float = 2.0) -> None:
        """End the child and reap it, then close the fd.

        A single SIGHUP plus one WNOHANG was not enough: if the child is still
        alive at that instant it is never reaped, and it becomes a zombie held
        by the agent for as long as the agent runs. The far side outliving its
        TTL is described elsewhere as the normal case, which is exactly when
        that fires. So: hang up, wait briefly, then SIGKILL, then reap for
        real.
        """
        if self.pid > 0:
            pid, self.pid = self.pid, -1
            for sig in (signal.SIGHUP, signal.SIGKILL):
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                if self._reap(pid, grace_seconds if sig == signal.SIGHUP else 1.0):
                    break
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1


class CockpitBridge:
    """The listener. Construction is cheap and side-effect free; ``start()``
    is what opens a port."""

    def __init__(self, config: BridgeConfig, *,
                 resolver: Callable[[int], Optional[Dict[str, Any]]],
                 token_verifier: Callable[[str], Any],
                 pty_factory: Callable[[Sequence[str]], PtySession] = PtySession,
                 audit: Optional[Callable[..., None]] = None):
        self.config = config
        self.resolver = resolver
        self.token_verifier = token_verifier
        self.pty_factory = pty_factory
        self.audit = audit
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        #: Set by the serving thread once the socket is bound, or once binding
        #: has failed. start() waits on it so "listening on :8084" is not
        #: printed by a bridge that lost the port to something else.
        self._ready = threading.Event()
        self._bind_error: Optional[BaseException] = None
        #: Open terminals, for the cap.
        self._sessions = 0
        self._sessions_lock = threading.Lock()

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> bool:
        """Open the port in a background thread. Returns whether it did.

        A disabled bridge does not bind, does not import websockets, and says
        nothing: an install that never turns this on should not have a port
        open, and should not be told about one it did not ask for.
        """
        if not self.config.enabled:
            return False
        if not self.config.allowed_origins:
            # Refuse at startup rather than at connect time. A bridge that is
            # listening but rejects every browser is the worst of both: the
            # port is open and nothing works.
            logger.error("cockpit bridge: enabled with no bridge_origins — "
                         "refusing to listen, since every connection would be "
                         "rejected. Set cockpit.bridge_origins to the console URL.")
            return False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="cockpit-bridge")
        self._thread.start()
        # Binding happens on that thread, so without this the caller logs
        # "listening" and an address-already-in-use lands seconds later, in a
        # different line, below the one an operator actually reads.
        if not self._ready.wait(BIND_TIMEOUT_SECONDS):
            logger.error("cockpit bridge: no answer from the listener after %ss",
                         BIND_TIMEOUT_SECONDS)
            return False
        if self._bind_error is not None:
            logger.error("cockpit bridge: could not listen on %s:%s — %s",
                         self.config.host, self.config.port, self._bind_error)
            return False
        logger.info("cockpit bridge listening on %s:%s (origins: %s, max %s)",
                    self.config.host, self.config.port,
                    ", ".join(self.config.allowed_origins),
                    self.config.max_sessions)
        return True

    def _run(self) -> None:  # pragma: no cover - exercised by hand, not in CI
        try:
            asyncio.run(self._serve())
        except Exception as e:
            # Record it for start(), which may still be waiting, then release
            # it either way — a bridge that died during bind must not leave the
            # agent's startup blocked on an Event that never gets set.
            self._bind_error = e
            logger.error("cockpit bridge stopped: %s", e, exc_info=True)
        finally:
            self._ready.set()

    async def _serve(self) -> None:  # pragma: no cover - needs a real socket
        import websockets

        self._loop = asyncio.get_running_loop()
        async with websockets.serve(self._handle, self.config.host,
                                    self.config.port,
                                    # The browser's Origin is checked in
                                    # authorize() along with everything else,
                                    # so the library's own check is off: one
                                    # place decides, and it is testable.
                                    origins=None,
                                    ping_interval=20, ping_timeout=20):
            # Bound. start() may now say so; the log line lives there so it is
            # printed once rather than by both halves.
            self._ready.set()
            await asyncio.Future()

    # ---- one connection --------------------------------------------------

    async def _handle(self, websocket) -> None:  # pragma: no cover - socket path
        path = getattr(getattr(websocket, "request", None), "path", "") or ""
        origin = ""
        headers = getattr(getattr(websocket, "request", None), "headers", None)
        if headers is not None:
            origin = headers.get("Origin") or ""

        try:
            raw = await asyncio.wait_for(websocket.recv(), AUTH_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, Exception):
            await self._refuse(websocket, CLOSE_UNAUTHENTICATED,
                               "no auth frame")
            return

        token = ""
        try:
            frame = json.loads(raw if isinstance(raw, str) else raw.decode())
            if isinstance(frame, dict) and frame.get("type") == "auth":
                token = str(frame.get("token") or "")
        except Exception:
            token = ""

        verdict = authorize(path=path, origin=origin, token=token,
                            config=self.config,
                            token_verifier=self.token_verifier,
                            resolver=self.resolver)
        if not verdict.ok:
            logger.warning("cockpit bridge refused a connection: %s", verdict.reason)
            await self._refuse(websocket, verdict.code, verdict.reason)
            return

        session = verdict.session or {}
        with self._sessions_lock:
            if self._sessions >= self.config.max_sessions:
                busy = self._sessions
                admitted = False
            else:
                self._sessions += 1
                admitted = True
        if not admitted:
            await self._refuse(
                websocket, CLOSE_BUSY,
                f"the bridge is already carrying {busy} terminals "
                f"(max {self.config.max_sessions})")
            return
        try:
            await self._pump(websocket, session, verdict.actor)
        finally:
            with self._sessions_lock:
                self._sessions -= 1

    async def _refuse(self, websocket, code: int, reason: str) -> None:  # pragma: no cover
        """Say why before closing.

        The close code carries the reason too, but a browser only reliably
        surfaces the code — the frame is what the console can put on screen.
        """
        try:
            await websocket.send(json.dumps({"type": "error", "code": code,
                                             "reason": reason}))
        except Exception:
            pass
        try:
            await websocket.close(code=code, reason=reason[:120])
        except Exception:
            pass

    async def _pump(self, websocket, session: Dict[str, Any], actor: str) -> None:  # pragma: no cover
        """Bytes in both directions until one end stops."""
        argv = list(session.get("attach_argv") or [])
        term = self.pty_factory(argv)
        term.start()
        self._record(session, actor, "opened")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _readable():
            try:
                data = os.read(term.fd, 65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                data = b""
            queue.put_nowait(data)

        loop.add_reader(term.fd, _readable)

        async def _to_browser():
            while True:
                data = await queue.get()
                if not data:
                    return
                await websocket.send(data)

        async def _from_browser():
            async for message in websocket:
                if isinstance(message, str):
                    # Control frames are text; terminal input is binary. That
                    # split means a resize can never be mistaken for someone
                    # typing the word "resize".
                    try:
                        frame = json.loads(message)
                    except Exception:
                        continue
                    if isinstance(frame, dict) and frame.get("type") == "resize":
                        term.resize(frame.get("cols", 80), frame.get("rows", 24))
                    continue
                await write_all(loop, term.fd, message)

        tasks = [asyncio.create_task(_to_browser()),
                 asyncio.create_task(_from_browser())]
        try:
            done, pending = await asyncio.wait(tasks,
                                               return_when=asyncio.FIRST_COMPLETED)
            # Finish the other half rather than abandoning it. A discarded task
            # is a "Task was destroyed but it is pending" at best, and at worst
            # a writer still parked on a socket — and every session on this
            # bridge shares one event loop.
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending)
            for task in done:
                if task.exception() is not None:
                    logger.warning("cockpit bridge session ended: %s",
                                   task.exception())
        finally:
            try:
                loop.remove_reader(term.fd)
            except Exception:
                pass
            term.close()
            self._record(session, actor, "closed")
            try:
                await websocket.close()
            except Exception:
                pass

    def _record(self, session: Dict[str, Any], actor: str, event: str) -> None:
        """Audit both ends of the session.

        Opening a terminal on a production host is exactly the thing someone
        asks about afterwards, and 'who' plus 'which machine' is the answer.
        """
        if not self.audit:
            return
        try:
            self.audit(event=f"cockpit_bridge_{event}", actor=actor,
                       investigation_id=session.get("investigation_id"),
                       tier=session.get("tier"), host=session.get("host"),
                       session_name=session.get("session_name"))
        except Exception as e:
            logger.warning("cockpit bridge audit failed: %s", e)
