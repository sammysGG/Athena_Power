import asyncio
import contextlib
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pymodbus.client import ModbusTcpClient


BRIDGE_API_KEY = os.environ["BRIDGE_API_KEY"]
SIMULATOR_API_KEY = os.environ["SIMULATOR_API_KEY"]
SIMULATOR_URL = os.getenv("SIMULATOR_URL", "http://simulator:9101")
MODBUS_HOST = os.getenv("MODBUS_HOST", "modbus-server")
MODBUS_PORT = int(os.getenv("MODBUS_PORT", "15020"))
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1.0"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "900"))
EVENT_LIMIT = int(os.getenv("EVENT_LIMIT", "2000"))
DB_PATH = os.getenv("BRIDGE_DB_PATH", "/tmp/telemetry.db")
DB_DIR = os.path.dirname(DB_PATH)
REMOTE_SETPOINT_REGISTER = 20
MAX_REMOTE_RPM = 14000

if DB_DIR:
    os.makedirs(DB_DIR, exist_ok=True)


EXPECTED_COLUMNS = [
    "timestamp",
    "rotor_enabled",
    "feed_enabled",
    "coolant_override",
    "emergency_trip",
    "rpm",
    "rpm_setpoint",
    "commanded_rpm",
    "remote_rpm_setpoint",
    "chamber_temp_c",
    "bearing_temp_c",
    "coolant_flow_lpm",
    "pressure_kpa",
    "vibration_mm_s",
    "power_kw",
    "temperature_margin_c",
    "alarm_text",
    "alarm_level",
    "batch_stage",
    "control_mode",
]


class BridgeState:
    def __init__(self) -> None:
        self.current_state: dict | None = None
        self.last_remote_setpoint: int = 0
        self.last_alarm_signature: tuple[str, str] | None = None
        self.last_rotor: bool | None = None
        self.last_feed: bool | None = None
        self.last_trip: bool | None = None
        self.last_coolant: bool | None = None
        self.last_mode: str | None = None
        self.lock = asyncio.Lock()


bridge_state = BridgeState()


def require_bridge_key(x_bridge_api_key: str = Header(default="")) -> None:
    if not secrets.compare_digest(x_bridge_api_key, BRIDGE_API_KEY):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bridge API key")


def create_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            rotor_enabled INTEGER NOT NULL,
            feed_enabled INTEGER NOT NULL,
            coolant_override INTEGER NOT NULL,
            emergency_trip INTEGER NOT NULL,
            rpm REAL NOT NULL,
            rpm_setpoint REAL NOT NULL,
            commanded_rpm REAL NOT NULL,
            remote_rpm_setpoint REAL NOT NULL,
            chamber_temp_c REAL NOT NULL,
            bearing_temp_c REAL NOT NULL,
            coolant_flow_lpm REAL NOT NULL,
            pressure_kpa REAL NOT NULL,
            vibration_mm_s REAL NOT NULL,
            power_kw REAL NOT NULL,
            temperature_margin_c REAL NOT NULL,
            alarm_text TEXT NOT NULL,
            alarm_level TEXT NOT NULL,
            batch_stage TEXT NOT NULL,
            control_mode TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            severity TEXT NOT NULL,
            source TEXT NOT NULL,
            tag TEXT NOT NULL,
            message TEXT NOT NULL,
            detail TEXT
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_events_id ON events(id DESC)")


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as connection:
        existing = connection.execute("PRAGMA table_info(telemetry)").fetchall()
        if existing:
            current_columns = [row[1] for row in existing if row[1] != "id"]
            if current_columns != EXPECTED_COLUMNS:
                connection.execute("DROP TABLE telemetry")
        create_table(connection)


def prune_history(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        DELETE FROM telemetry
        WHERE id NOT IN (
            SELECT id FROM telemetry
            ORDER BY id DESC
            LIMIT ?
        )
        """,
        (HISTORY_LIMIT,),
    )


def prune_events(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        DELETE FROM events
        WHERE id NOT IN (
            SELECT id FROM events
            ORDER BY id DESC
            LIMIT ?
        )
        """,
        (EVENT_LIMIT,),
    )


def insert_event(severity: str, source: str, tag: str, message: str, detail: str | None = None) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            "INSERT INTO events (timestamp, severity, source, tag, message, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, severity, source, tag, message, detail),
        )
        prune_events(connection)


def persist_state(state: dict) -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            INSERT INTO telemetry (
                timestamp,
                rotor_enabled,
                feed_enabled,
                coolant_override,
                emergency_trip,
                rpm,
                rpm_setpoint,
                commanded_rpm,
                remote_rpm_setpoint,
                chamber_temp_c,
                bearing_temp_c,
                coolant_flow_lpm,
                pressure_kpa,
                vibration_mm_s,
                power_kw,
                temperature_margin_c,
                alarm_text,
                alarm_level,
                batch_stage,
                control_mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state["timestamp"],
                int(bool(state["rotor_enabled"])),
                int(bool(state["feed_enabled"])),
                int(bool(state["coolant_override"])),
                int(bool(state["emergency_trip"])),
                float(state["rpm"]),
                float(state["rpm_setpoint"]),
                float(state["commanded_rpm"]),
                float(state["remote_rpm_setpoint"]),
                float(state["chamber_temp_c"]),
                float(state["bearing_temp_c"]),
                float(state["coolant_flow_lpm"]),
                float(state["pressure_kpa"]),
                float(state["vibration_mm_s"]),
                float(state["power_kw"]),
                float(state["temperature_margin_c"]),
                state["alarm_text"],
                state["alarm_level"],
                state["batch_stage"],
                state["control_mode"],
            ),
        )
        prune_history(connection)


def get_history(limit: int = 120) -> list[dict]:
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT timestamp, rotor_enabled, feed_enabled, coolant_override, emergency_trip, rpm, rpm_setpoint,
                   commanded_rpm, remote_rpm_setpoint, chamber_temp_c, bearing_temp_c, coolant_flow_lpm,
                   pressure_kpa, vibration_mm_s, power_kw, temperature_margin_c, alarm_text, alarm_level,
                   batch_stage, control_mode
            FROM telemetry
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def get_events(limit: int = 200) -> list[dict]:
    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, timestamp, severity, source, tag, message, detail FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def update_modbus(state: dict) -> None:
    client = ModbusTcpClient(host=MODBUS_HOST, port=MODBUS_PORT)
    try:
        if not client.connect():
            return

        client.write_coils(
            0,
            [
                bool(state["rotor_enabled"]),
                bool(state["feed_enabled"]),
                bool(state["coolant_override"]),
                bool(state["emergency_trip"]),
            ],
        )
        client.write_registers(
            0,
            [
                int(round(float(state["rpm"]))),
                int(round(float(state["rpm_setpoint"]))),
                int(round(float(state["chamber_temp_c"]) * 10)),
                int(round(float(state["bearing_temp_c"]) * 10)),
                int(round(float(state["coolant_flow_lpm"]) * 10)),
                int(round(float(state["pressure_kpa"]) * 10)),
                int(round(float(state["vibration_mm_s"]) * 100)),
                int(round(float(state["power_kw"]) * 10)),
                int(round(float(state["temperature_margin_c"]) * 10)),
                {"normal": 0, "warning": 1, "trip": 2}.get(state["alarm_level"], 3),
                int(round(float(state["commanded_rpm"]))),
                int(round(float(state["remote_rpm_setpoint"]))),
                {"STANDBY": 0, "AUTO GOVERNOR": 1, "FIELD OVERRIDE": 2, "TRIP LOCKOUT": 3}.get(state["control_mode"], 4),
                int(round(float(state.get("motor_current_a", 0.0)) * 10)),
                int(round(float(state.get("feed_pressure_kpa", 0.0)) * 10)),
                int(round(float(state.get("chamber_b_temp_c", 0.0)) * 10)),
                int(round(float(state.get("chamber_c_temp_c", 0.0)) * 10)),
                int(round(float(state.get("feed_tank_level_pct", 0.0)) * 10)),
                int(round(float(state.get("product_tank_level_pct", 0.0)) * 10)),
                int(round(float(state.get("lube_pressure_kpa", 0.0)) * 10)),
            ],
        )
    finally:
        client.close()


def write_remote_setpoint_register(rpm: int) -> None:
    client = ModbusTcpClient(host=MODBUS_HOST, port=MODBUS_PORT)
    try:
        if not client.connect():
            return
        client.write_register(REMOTE_SETPOINT_REGISTER, int(max(0, min(rpm, MAX_REMOTE_RPM))))
    finally:
        client.close()


def read_remote_setpoint() -> int | None:
    client = ModbusTcpClient(host=MODBUS_HOST, port=MODBUS_PORT)
    try:
        if not client.connect():
            return None
        response = client.read_holding_registers(REMOTE_SETPOINT_REGISTER, count=1)
        if response.isError():
            return None
        value = int(response.registers[0])
        if value < 500:
            return 0
        return min(value, MAX_REMOTE_RPM)
    finally:
        client.close()


def dump_modbus_map() -> dict:
    """Snapshot of the modbus register map. Exposed via /internal/diagnostics."""
    client = ModbusTcpClient(host=MODBUS_HOST, port=MODBUS_PORT)
    try:
        if not client.connect():
            return {"connected": False}
        hr = client.read_holding_registers(0, count=32)
        co = client.read_coils(0, count=8)
        return {
            "connected": True,
            "host": MODBUS_HOST,
            "port": MODBUS_PORT,
            "holding_registers": list(hr.registers) if not hr.isError() else [],
            "coils": list(co.bits[:8]) if not co.isError() else [],
        }
    finally:
        client.close()


def _emit_transition_events(prev: BridgeState, new_state: dict) -> None:
    """Compare new state to previous and emit SOE events on transitions."""
    rotor = bool(new_state["rotor_enabled"])
    feed = bool(new_state["feed_enabled"])
    trip = bool(new_state["emergency_trip"])
    coolant = bool(new_state["coolant_override"])
    mode = str(new_state.get("control_mode", ""))
    alarm_text = str(new_state.get("alarm_text", ""))
    alarm_level = str(new_state.get("alarm_level", "normal"))

    if prev.last_rotor is not None and rotor != prev.last_rotor:
        insert_event("info", "PLANT", "XS-301", f"Rotor train {'STARTED' if rotor else 'STOPPED'}")
    if prev.last_feed is not None and feed != prev.last_feed:
        insert_event("info", "PLANT", "FCV-101", f"Feed valve {'OPENED' if feed else 'CLOSED'}")
    if prev.last_coolant is not None and coolant != prev.last_coolant:
        insert_event("info", "PLANT", "FIC-401", f"Coolant override {'ENGAGED' if coolant else 'RELEASED'}")
    if prev.last_trip is not None and trip != prev.last_trip:
        if trip:
            insert_event("alarm", "PLANT", "XA-301", f"EMERGENCY TRIP LATCHED — {alarm_text}")
        else:
            insert_event("info", "PLANT", "XA-301", "Trip latch reset to STANDBY")
    if prev.last_mode is not None and mode != prev.last_mode:
        insert_event("info", "PLANT", "TIC-301", f"Control mode → {mode}")

    sig = (alarm_text, alarm_level)
    if prev.last_alarm_signature != sig and alarm_level in ("warning", "trip"):
        sev = "alarm" if alarm_level == "trip" else "warn"
        insert_event(sev, "PLANT", "CF-301", alarm_text or alarm_level.upper())

    prev.last_rotor = rotor
    prev.last_feed = feed
    prev.last_trip = trip
    prev.last_coolant = coolant
    prev.last_mode = mode
    prev.last_alarm_signature = sig


async def fetch_simulator_state(client: httpx.AsyncClient) -> dict:
    response = await client.get(
        f"{SIMULATOR_URL}/state",
        headers={"X-Simulator-Api-Key": SIMULATOR_API_KEY},
        timeout=5.0,
    )
    response.raise_for_status()
    state = response.json()
    await asyncio.to_thread(update_modbus, state)
    await asyncio.to_thread(persist_state, state)
    async with bridge_state.lock:
        _emit_transition_events(bridge_state, state)
        bridge_state.current_state = state
    return state


async def proxy_command_with_client(client: httpx.AsyncClient, path: str, payload: dict | None = None) -> dict:
    response = await client.post(
        f"{SIMULATOR_URL}{path}",
        headers={"X-Simulator-Api-Key": SIMULATOR_API_KEY},
        json=payload or {},
        timeout=5.0,
    )
    response.raise_for_status()
    state = response.json()
    await asyncio.to_thread(update_modbus, state)
    await asyncio.to_thread(persist_state, state)
    async with bridge_state.lock:
        _emit_transition_events(bridge_state, state)
        bridge_state.current_state = state
    return state


async def proxy_command(path: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient() as client:
        return await proxy_command_with_client(client, path, payload)


async def bridge_loop() -> None:
    async with httpx.AsyncClient() as client:
        while True:
            try:
                remote_setpoint = await asyncio.to_thread(read_remote_setpoint)
                if remote_setpoint is not None:
                    async with bridge_state.lock:
                        last_remote_setpoint = bridge_state.last_remote_setpoint
                    if remote_setpoint != last_remote_setpoint:
                        # NOTE: HR20 is writable over Modbus by anything that
                        # can reach the OT network. Bridge cannot distinguish
                        # an HMI-originated write from an unauthorised one,
                        # so every change is recorded for the SOE log.
                        await asyncio.to_thread(
                            insert_event,
                            "warn",
                            "MODBUS",
                            "HR20",
                            f"Remote setpoint register changed: {last_remote_setpoint} → {remote_setpoint} RPM",
                            "writer=unknown (Modbus has no authentication)",
                        )
                        await proxy_command_with_client(client, "/command/remote-setpoint", {"rpm": remote_setpoint})
                        async with bridge_state.lock:
                            bridge_state.last_remote_setpoint = remote_setpoint
                    else:
                        await fetch_simulator_state(client)
                else:
                    await fetch_simulator_state(client)
            except Exception:
                pass
            await asyncio.sleep(POLL_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    insert_event("info", "BRIDGE", "SYS", "Bridge service started")
    task = asyncio.create_task(bridge_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="scada-bridge", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    async with bridge_state.lock:
        current = bridge_state.current_state
    return {"status": "ok", "has_state": current is not None}


@app.get("/internal/state", dependencies=[Depends(require_bridge_key)])
async def current_state() -> dict:
    async with bridge_state.lock:
        if bridge_state.current_state is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="state unavailable")
        return bridge_state.current_state


@app.get("/internal/history", dependencies=[Depends(require_bridge_key)])
async def history(limit: int = 120) -> dict:
    bounded_limit = max(10, min(limit, HISTORY_LIMIT))
    return {"items": await asyncio.to_thread(get_history, bounded_limit)}


@app.get("/internal/events", dependencies=[Depends(require_bridge_key)])
async def events(limit: int = 200) -> dict:
    bounded = max(10, min(limit, EVENT_LIMIT))
    return {"items": await asyncio.to_thread(get_events, bounded)}


@app.get("/internal/diagnostics", dependencies=[Depends(require_bridge_key)])
async def diagnostics() -> dict:
    modbus = await asyncio.to_thread(dump_modbus_map)
    async with bridge_state.lock:
        state = bridge_state.current_state
        last_sp = bridge_state.last_remote_setpoint
    return {
        "service": "scada-bridge",
        "modbus": modbus,
        "modbus_register_map": {
            "HR0": "rpm",
            "HR1": "rpm_setpoint (auto)",
            "HR2": "chamber_temp_c * 10",
            "HR3": "bearing_temp_c * 10",
            "HR4": "coolant_flow_lpm * 10",
            "HR5": "pressure_kpa * 10",
            "HR6": "vibration_mm_s * 100",
            "HR7": "power_kw * 10",
            "HR8": "temperature_margin_c * 10",
            "HR9": "alarm_level enum",
            "HR10": "commanded_rpm",
            "HR11": "remote_rpm_setpoint mirror",
            "HR12": "control_mode enum",
            "HR13": "motor_current_a * 10",
            "HR14": "feed_pressure_kpa * 10",
            "HR15": "chamber_b_temp_c * 10",
            "HR16": "chamber_c_temp_c * 10",
            "HR17": "feed_tank_level_pct * 10",
            "HR18": "product_tank_level_pct * 10",
            "HR19": "lube_pressure_kpa * 10",
            "HR20": "REMOTE SPEED SETPOINT (writable)",
            "CO0": "rotor_enabled",
            "CO1": "feed_enabled",
            "CO2": "coolant_override",
            "CO3": "emergency_trip",
        },
        "poll_seconds": POLL_SECONDS,
        "last_remote_setpoint": last_sp,
        "history_limit": HISTORY_LIMIT,
        "event_limit": EVENT_LIMIT,
        "has_state": state is not None,
    }


@app.post("/internal/command/rotor", dependencies=[Depends(require_bridge_key)])
async def command_rotor(payload: dict) -> dict:
    if "enabled" not in payload:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="enabled is required")
    actor = payload.get("__actor", "unknown")
    await asyncio.to_thread(insert_event, "info", "HMI", "XS-301", f"Rotor command: {'START' if payload['enabled'] else 'STOP'}", f"actor={actor}")
    return await proxy_command("/command/rotor", {"enabled": bool(payload["enabled"])})


@app.post("/internal/command/feed", dependencies=[Depends(require_bridge_key)])
async def command_feed(payload: dict) -> dict:
    if "enabled" not in payload:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="enabled is required")
    actor = payload.get("__actor", "unknown")
    await asyncio.to_thread(insert_event, "info", "HMI", "FCV-101", f"Feed command: {'OPEN' if payload['enabled'] else 'CLOSE'}", f"actor={actor}")
    return await proxy_command("/command/feed", {"enabled": bool(payload["enabled"])})


@app.post("/internal/command/coolant-override", dependencies=[Depends(require_bridge_key)])
async def command_coolant_override(payload: dict) -> dict:
    if "enabled" not in payload:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="enabled is required")
    actor = payload.get("__actor", "unknown")
    await asyncio.to_thread(insert_event, "info", "HMI", "FIC-401", f"Coolant override: {'ENGAGE' if payload['enabled'] else 'RELEASE'}", f"actor={actor}")
    return await proxy_command("/command/coolant-override", {"enabled": bool(payload["enabled"])})


@app.post("/internal/command/trip", dependencies=[Depends(require_bridge_key)])
async def command_trip(payload: dict | None = None) -> dict:
    actor = (payload or {}).get("__actor", "unknown")
    await asyncio.to_thread(insert_event, "alarm", "HMI", "XA-301", "Manual E-Stop initiated", f"actor={actor}")
    return await proxy_command("/command/trip")


@app.post("/internal/command/reset-trip", dependencies=[Depends(require_bridge_key)])
async def command_reset_trip(payload: dict | None = None) -> dict:
    actor = (payload or {}).get("__actor", "unknown")
    await asyncio.to_thread(insert_event, "info", "HMI", "XA-301", "Trip reset requested", f"actor={actor}")
    return await proxy_command("/command/reset-trip")


@app.post("/internal/command/remote-setpoint", dependencies=[Depends(require_bridge_key)])
async def command_remote_setpoint(payload: dict) -> dict:
    if "rpm" not in payload:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="rpm is required")
    rpm = int(float(payload["rpm"]))
    actor = payload.get("__actor", "unknown")
    await asyncio.to_thread(insert_event, "info", "HMI", "SI-301.SP", f"Remote setpoint write: {rpm} RPM", f"actor={actor}")
    await asyncio.to_thread(write_remote_setpoint_register, rpm)
    async with bridge_state.lock:
        bridge_state.last_remote_setpoint = 0 if rpm < 500 else min(rpm, MAX_REMOTE_RPM)
    return await proxy_command("/command/remote-setpoint", {"rpm": rpm})
