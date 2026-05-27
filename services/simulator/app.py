import asyncio
import math
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, status


SAFE_RPM = 8600.0
BASE_RPM_SETPOINT = 8600.0
MAX_REMOTE_RPM = 14000.0
MIN_REMOTE_RPM = 6800.0
MIN_FEED_RPM = 7200.0
TRIP_CHAMBER_TEMP_C = 92.0
TRIP_BEARING_TEMP_C = 96.0
TRIP_VIBRATION_MM_S = 6.6
TRIP_PRESSURE_KPA = 165.0
TRIP_OVERSPEED_RPM = 12800.0


class PlantState:
    def __init__(self) -> None:
        self.rotor_enabled = False
        self.feed_enabled = False
        self.coolant_override = False
        self.emergency_trip = False
        self.sequence = 0
        self.phase = 0.0
        self.rpm = 0.0
        self.commanded_rpm = 0.0
        self.remote_rpm_setpoint = 0.0
        self.control_mode = "STANDBY"
        self.chamber_temp_c = 31.0
        self.bearing_temp_c = 31.8
        self.coolant_flow_lpm = 32.0
        self.pressure_kpa = 103.0
        self.vibration_mm_s = 0.65
        self.power_kw = 6.0
        # New process variables (additional, do not break existing physics)
        self.motor_current_a = 12.0
        self.feed_pressure_kpa = 96.0
        self.chamber_b_temp_c = 30.4
        self.chamber_c_temp_c = 29.6
        self.feed_tank_level_pct = 78.0
        self.product_tank_level_pct = 12.0
        self.lube_pressure_kpa = 240.0
        self.alarm_text = "STANDBY"
        self.alarm_level = "normal"
        self.batch_stage = "IDLE"
        self.last_updated = datetime.now(timezone.utc)
        self._lock = asyncio.Lock()

    def _apply_trip(self, reason: str) -> None:
        self.emergency_trip = True
        self.rotor_enabled = False
        self.feed_enabled = False
        self.coolant_override = True
        self.commanded_rpm = 0.0
        self.alarm_text = reason
        self.alarm_level = "trip"
        self.batch_stage = "TRIPPED"

    def _resolve_rotor_target(self) -> float:
        if self.emergency_trip:
            self.control_mode = "TRIP LOCKOUT"
            self.commanded_rpm = 0.0
            return 0.0

        if not self.rotor_enabled:
            self.control_mode = "STANDBY"
            self.commanded_rpm = 0.0
            return 0.0

        if self.remote_rpm_setpoint >= MIN_REMOTE_RPM:
            self.control_mode = "FIELD OVERRIDE"
            self.commanded_rpm = min(self.remote_rpm_setpoint, MAX_REMOTE_RPM)
            return self.commanded_rpm

        thermal_trim = max(self.chamber_temp_c - 56.0, 0.0) * 95.0
        thermal_trim += max(self.bearing_temp_c - 61.0, 0.0) * 72.0
        thermal_trim += max(self.vibration_mm_s - 3.2, 0.0) * 180.0
        thermal_trim += max(self.pressure_kpa - 134.0, 0.0) * 22.0

        self.control_mode = "AUTO GOVERNOR"
        self.commanded_rpm = max(7600.0, BASE_RPM_SETPOINT - thermal_trim)
        return self.commanded_rpm

    def _update_process(self) -> None:
        self.phase = (self.phase + 0.17) % (math.pi * 2)
        self.sequence += 1

        rotor_target = self._resolve_rotor_target()
        feed_target = bool(self.feed_enabled and self.rotor_enabled and not self.emergency_trip)
        self.rpm += (rotor_target - self.rpm) * 0.18

        overspeed_factor = max(self.rpm - SAFE_RPM, 0.0) / 1800.0
        coolant_target = 30.0
        coolant_target += min(self.rpm / 340.0, 27.0)
        coolant_target += max(self.chamber_temp_c - 44.0, 0.0) * 1.45
        coolant_target += max(self.bearing_temp_c - 50.0, 0.0) * 0.95
        coolant_target += 11.0 if self.coolant_override else 0.0
        coolant_target -= 3.5 if not self.rotor_enabled else 0.0
        self.coolant_flow_lpm += (coolant_target - self.coolant_flow_lpm) * 0.24 + math.sin(self.phase * 1.1) * 0.35
        self.coolant_flow_lpm = min(max(self.coolant_flow_lpm, 20.0), 88.0)

        heat_gain = 0.03 + (self.rpm / SAFE_RPM) * 0.82 + overspeed_factor * 3.1 + (0.55 if feed_target else 0.0)
        heat_loss = 0.42 + (self.coolant_flow_lpm / 100.0) * 1.62
        self.chamber_temp_c += heat_gain - heat_loss + math.sin(self.phase * 0.45) * 0.04
        self.chamber_temp_c = max(self.chamber_temp_c, 27.5)

        bearing_target = self.chamber_temp_c + (self.rpm / SAFE_RPM) * 6.9 + overspeed_factor * 18.0 + (1.2 if feed_target else 0.0)
        self.bearing_temp_c += (bearing_target - self.bearing_temp_c) * 0.18

        pressure_target = 101.0 + (self.rpm / SAFE_RPM) * 30.0 + overspeed_factor * 19.0 + (6.5 if feed_target else 0.0)
        pressure_target -= 6.0 if self.coolant_override else 0.0
        self.pressure_kpa += (pressure_target - self.pressure_kpa) * 0.17 + math.cos(self.phase * 0.7) * 0.24

        vibration_target = 0.6 + (self.rpm / SAFE_RPM) * 2.55 + overspeed_factor * 2.0 + (0.7 if feed_target else 0.0)
        vibration_target += 0.75 if self.control_mode == "FIELD OVERRIDE" and self.rpm >= 9800.0 else 0.0
        self.vibration_mm_s += (vibration_target - self.vibration_mm_s) * 0.2 + abs(math.sin(self.phase * 1.9)) * 0.06

        power_target = 6.0 + (self.rpm / SAFE_RPM) * 124.0 + overspeed_factor * 46.0 + (8.0 if feed_target else 0.0)
        power_target += 7.0 if self.coolant_override else 0.0
        self.power_kw += (power_target - self.power_kw) * 0.2

        current_target = 9.0 + (self.power_kw / 12.0) + overspeed_factor * 18.0
        self.motor_current_a += (current_target - self.motor_current_a) * 0.22 + math.sin(self.phase * 1.3) * 0.15

        feed_pressure_target = 96.0 + (18.0 if feed_target else 0.0) + max(self.pressure_kpa - 110.0, 0.0) * 0.18
        self.feed_pressure_kpa += (feed_pressure_target - self.feed_pressure_kpa) * 0.2 + math.cos(self.phase * 0.9) * 0.18

        chamber_b_target = self.chamber_temp_c - 0.6 + math.sin(self.phase * 0.5) * 0.10
        chamber_c_target = self.chamber_temp_c - 1.4 + math.cos(self.phase * 0.5) * 0.10
        self.chamber_b_temp_c += (chamber_b_target - self.chamber_b_temp_c) * 0.25
        self.chamber_c_temp_c += (chamber_c_target - self.chamber_c_temp_c) * 0.25

        feed_draw = 0.018 if feed_target else 0.0
        feed_makeup = 0.012 if (self.feed_tank_level_pct < 30 and not self.emergency_trip) else 0.0
        self.feed_tank_level_pct = min(max(self.feed_tank_level_pct - feed_draw + feed_makeup, 0.0), 100.0)
        prod_fill = 0.012 if feed_target else 0.0
        self.product_tank_level_pct = min(max(self.product_tank_level_pct + prod_fill, 0.0), 100.0)

        lube_target = 240.0 if self.rotor_enabled and not self.emergency_trip else 60.0
        lube_target -= overspeed_factor * 30.0
        self.lube_pressure_kpa += (lube_target - self.lube_pressure_kpa) * 0.25

        if self.emergency_trip:
            self.alarm_level = "trip"
            self.batch_stage = "TRIPPED"
            self.alarm_text = "TRIP LATCHED - COOLING DOWN" if self.rpm < 300.0 else "EMERGENCY SHUTDOWN ACTIVE"
        else:
            self.batch_stage = "IDLE"
            self.alarm_level = "normal"
            self.alarm_text = "STANDBY"

            if self.rpm > 200.0 and self.rpm < MIN_FEED_RPM:
                self.batch_stage = "SPIN-UP"
                self.alarm_text = "CASCADE DEPLOYING"
            elif self.rpm >= MIN_FEED_RPM and feed_target and self.control_mode == "AUTO GOVERNOR":
                self.batch_stage = "SEPARATION"
                self.alarm_text = "AUTO CASCADE STABLE"
            elif self.rpm >= MIN_FEED_RPM and self.control_mode == "FIELD OVERRIDE":
                self.batch_stage = "FIELD OVERRIDE"
                self.alarm_text = "REMOTE SPEED COMMAND ACTIVE"
            elif self.rpm >= MIN_FEED_RPM:
                self.batch_stage = "READY"
                self.alarm_text = "READY FOR FEED"

            if self.chamber_temp_c >= TRIP_CHAMBER_TEMP_C:
                self._apply_trip("CHAMBER TEMP CRITICAL")
            elif self.bearing_temp_c >= TRIP_BEARING_TEMP_C:
                self._apply_trip("BEARING TEMP CRITICAL")
            elif self.vibration_mm_s >= TRIP_VIBRATION_MM_S:
                self._apply_trip("VIBRATION CRITICAL")
            elif self.pressure_kpa >= TRIP_PRESSURE_KPA:
                self._apply_trip("PRESSURE CRITICAL")
            elif self.rpm >= TRIP_OVERSPEED_RPM:
                self._apply_trip("OVERSPEED CRITICAL")
            elif self.control_mode == "FIELD OVERRIDE" and self.commanded_rpm > SAFE_RPM:
                self.alarm_level = "warning"
                self.alarm_text = "FIELD SETPOINT ABOVE SAFE BAND"
            elif self.chamber_temp_c >= 80.0:
                self.alarm_level = "warning"
                self.alarm_text = "CHAMBER TEMP RISING"
            elif self.bearing_temp_c >= 84.0:
                self.alarm_level = "warning"
                self.alarm_text = "BEARING TEMP RISING"
            elif self.vibration_mm_s >= 4.8:
                self.alarm_level = "warning"
                self.alarm_text = "VIBRATION TREND HIGH"
            elif self.pressure_kpa >= 146.0:
                self.alarm_level = "warning"
                self.alarm_text = "PRESSURE BAND HIGH"
            elif self.coolant_override:
                self.alarm_level = "warning"
                self.alarm_text = "COOLANT OVERRIDE ACTIVE"

        self.last_updated = datetime.now(timezone.utc)

    def _snapshot_payload(self) -> dict:
        temperature_margin = max(TRIP_CHAMBER_TEMP_C - self.chamber_temp_c, 0.0)
        return {
            "site": os.getenv("SCADA_SITE_NAME", "GoatHost Range SCADA"),
            "rotor_enabled": self.rotor_enabled,
            "feed_enabled": self.feed_enabled,
            "coolant_override": self.coolant_override,
            "emergency_trip": self.emergency_trip,
            "alarm_text": self.alarm_text,
            "alarm_level": self.alarm_level,
            "batch_stage": self.batch_stage,
            "control_mode": self.control_mode,
            "rpm": round(self.rpm, 0),
            "rpm_setpoint": round(self.commanded_rpm, 0),
            "commanded_rpm": round(self.commanded_rpm, 0),
            "remote_rpm_setpoint": round(self.remote_rpm_setpoint, 0),
            "chamber_temp_c": round(self.chamber_temp_c, 2),
            "chamber_b_temp_c": round(self.chamber_b_temp_c, 2),
            "chamber_c_temp_c": round(self.chamber_c_temp_c, 2),
            "bearing_temp_c": round(self.bearing_temp_c, 2),
            "coolant_flow_lpm": round(self.coolant_flow_lpm, 2),
            "pressure_kpa": round(self.pressure_kpa, 2),
            "feed_pressure_kpa": round(self.feed_pressure_kpa, 2),
            "lube_pressure_kpa": round(self.lube_pressure_kpa, 2),
            "vibration_mm_s": round(self.vibration_mm_s, 2),
            "power_kw": round(self.power_kw, 1),
            "motor_current_a": round(self.motor_current_a, 1),
            "feed_tank_level_pct": round(self.feed_tank_level_pct, 1),
            "product_tank_level_pct": round(self.product_tank_level_pct, 1),
            "temperature_margin_c": round(temperature_margin, 2),
            "sequence": self.sequence,
            "timestamp": self.last_updated.isoformat(),
        }

    async def snapshot(self) -> dict:
        async with self._lock:
            self._update_process()
            return self._snapshot_payload()

    async def set_rotor(self, enabled: bool) -> dict:
        async with self._lock:
            if enabled and self.emergency_trip:
                self.alarm_text = "RESET TRIP BEFORE RESTART"
                self.alarm_level = "trip"
            else:
                self.rotor_enabled = enabled
                if not enabled:
                    self.feed_enabled = False
            self._update_process()
            return self._snapshot_payload()

    async def set_feed(self, enabled: bool) -> dict:
        async with self._lock:
            self.feed_enabled = bool(enabled and not self.emergency_trip and self.rpm >= MIN_FEED_RPM)
            self._update_process()
            return self._snapshot_payload()

    async def set_coolant_override(self, enabled: bool) -> dict:
        async with self._lock:
            self.coolant_override = enabled
            self._update_process()
            return self._snapshot_payload()

    async def set_remote_setpoint(self, rpm: float) -> dict:
        async with self._lock:
            if rpm <= 0:
                self.remote_rpm_setpoint = 0.0
            else:
                self.remote_rpm_setpoint = min(max(rpm, MIN_REMOTE_RPM), MAX_REMOTE_RPM)
            self._update_process()
            return self._snapshot_payload()

    async def trip(self) -> dict:
        async with self._lock:
            self._apply_trip("MANUAL E-SHUTDOWN")
            self._update_process()
            return self._snapshot_payload()

    async def reset_trip(self) -> dict:
        async with self._lock:
            if self.rpm < 400 and self.chamber_temp_c < 58.0 and self.bearing_temp_c < 62.0:
                self.emergency_trip = False
                self.coolant_override = False
                self.alarm_text = "TRIP RESET - STANDBY"
                self.alarm_level = "normal"
                self.batch_stage = "IDLE"
            else:
                self.alarm_text = "TRIP RESET BLOCKED - COOL DOWN REQUIRED"
                self.alarm_level = "trip"
            self._update_process()
            return self._snapshot_payload()


state = PlantState()
simulator_api_key = os.environ["SCADA_SIMULATOR_API_KEY"]


def require_api_key(x_simulator_api_key: str = Header(default="")) -> None:
    if not secrets.compare_digest(x_simulator_api_key, simulator_api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid simulator API key")


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield


app = FastAPI(title="scada-simulator", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/state", dependencies=[Depends(require_api_key)])
async def get_state() -> dict:
    return await state.snapshot()


@app.post("/command/rotor", dependencies=[Depends(require_api_key)])
async def set_rotor(payload: dict) -> dict:
    if "enabled" not in payload:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="enabled is required")
    return await state.set_rotor(bool(payload["enabled"]))


@app.post("/command/feed", dependencies=[Depends(require_api_key)])
async def set_feed(payload: dict) -> dict:
    if "enabled" not in payload:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="enabled is required")
    return await state.set_feed(bool(payload["enabled"]))


@app.post("/command/coolant-override", dependencies=[Depends(require_api_key)])
async def set_coolant_override(payload: dict) -> dict:
    if "enabled" not in payload:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="enabled is required")
    return await state.set_coolant_override(bool(payload["enabled"]))


@app.post("/command/remote-setpoint", dependencies=[Depends(require_api_key)])
async def set_remote_setpoint(payload: dict) -> dict:
    if "rpm" not in payload:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="rpm is required")
    return await state.set_remote_setpoint(float(payload["rpm"]))


@app.post("/command/trip", dependencies=[Depends(require_api_key)])
async def trip(_: dict) -> dict:
    return await state.trip()


@app.post("/command/reset-trip", dependencies=[Depends(require_api_key)])
async def reset_trip(_: dict) -> dict:
    return await state.reset_trip()
