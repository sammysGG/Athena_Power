import asyncio
import os

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext
from pymodbus.server import StartAsyncTcpServer


async def main() -> None:
    port = int(os.getenv("MODBUS_PORT", "15020"))
    holding_registers = [0] * 64
    input_registers = [0] * 64

    # Seed the holding-register map. The bridge keeps it updated; these are
    # only initial values so a modbus client can introspect the map at boot.
    # HR0  rpm
    # HR1  rpm_setpoint (auto cascade)
    # HR2  chamber_temp_c * 10
    # HR3  bearing_temp_c * 10
    # HR4  coolant_flow_lpm * 10
    # HR5  pressure_kpa * 10
    # HR6  vibration_mm_s * 100
    # HR7  power_kw * 10
    # HR8  temperature_margin_c * 10
    # HR9  alarm_level (0=normal, 1=warning, 2=trip)
    # HR10 commanded_rpm
    # HR11 remote_rpm_setpoint (mirror of HR20)
    # HR12 control_mode enum
    # HR13 motor_current_a * 10
    # HR14 feed_pressure_kpa * 10
    # HR15 chamber_b_temp_c * 10
    # HR16 chamber_c_temp_c * 10
    # HR17 feed_tank_level_pct * 10
    # HR18 product_tank_level_pct * 10
    # HR19 lube_pressure_kpa * 10
    # HR20 REMOTE SPEED SETPOINT (operator/remote write target) — bridge polls this
    holding_registers[2] = 310
    holding_registers[3] = 318
    holding_registers[4] = 320
    holding_registers[5] = 1030
    holding_registers[6] = 65
    holding_registers[7] = 60
    holding_registers[8] = 610
    holding_registers[13] = 120
    holding_registers[14] = 960
    holding_registers[15] = 304
    holding_registers[16] = 296
    holding_registers[17] = 780
    holding_registers[18] = 120
    holding_registers[19] = 2400
    holding_registers[20] = 0
    input_registers[:] = holding_registers[:]

    store = ModbusSlaveContext(
        di=ModbusSequentialDataBlock(0, [0] * 16),
        co=ModbusSequentialDataBlock(0, [0, 0, 0, 0] + [0] * 12),
        hr=ModbusSequentialDataBlock(0, holding_registers),
        ir=ModbusSequentialDataBlock(0, input_registers),
    )
    context = ModbusServerContext(slaves=store, single=True)
    await StartAsyncTcpServer(context=context, address=("0.0.0.0", port))


if __name__ == "__main__":
    asyncio.run(main())
